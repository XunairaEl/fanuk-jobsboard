"""FAN-UK jobs board scraper.

Reads the company list from the Notion Companies database, fetches each
company's public careers feed (Greenhouse / Ashby / Lever), filters roles to
UK locations with a tech/CS fit, and syncs the Notion Jobs database:
new roles are created, roles that vanished from a feed are marked Closed.

A company whose feed fails to fetch is skipped entirely — its existing jobs
are left untouched so a transient outage never mass-closes listings.

Usage:
    python scrape.py             # full run (needs NOTION_TOKEN)
    python scrape.py --dry-run   # fetch + filter only, no Notion access
"""

import argparse
import datetime
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests
import yaml

import notion_api as notion

FEED_TIMEOUT = 30
UK_WORD_RE = re.compile(r"\buk\b|\bu\.k\.?\b", re.IGNORECASE)


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


# --- feed adapters -----------------------------------------------------------
# Each adapter returns a list of dicts:
#   {"id", "title", "location", "department", "url"}

def fetch_greenhouse(slug):
    resp = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        params={"content": "true"}, timeout=FEED_TIMEOUT)
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        departments = [d["name"] for d in j.get("departments", []) if d.get("name")]
        jobs.append({
            "id": str(j["id"]),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "department": ", ".join(departments),
            "url": j.get("absolute_url", ""),
        })
    return jobs


def fetch_ashby(slug):
    resp = requests.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        timeout=FEED_TIMEOUT)
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        locations = [j.get("location") or ""]
        locations += [s.get("location", "") for s in j.get("secondaryLocations", [])]
        jobs.append({
            "id": str(j["id"]),
            "title": j.get("title", ""),
            "location": "; ".join(loc for loc in locations if loc),
            "department": j.get("department") or j.get("team") or "",
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
        })
    return jobs


def fetch_lever(slug):
    resp = requests.get(
        f"https://api.lever.co/v0/postings/{slug}",
        params={"mode": "json"}, timeout=FEED_TIMEOUT)
    resp.raise_for_status()
    jobs = []
    for j in resp.json():
        cats = j.get("categories") or {}
        locations = [cats.get("location") or ""] + (cats.get("allLocations") or [])
        jobs.append({
            "id": str(j["id"]),
            "title": j.get("text", ""),
            "location": "; ".join(dict.fromkeys(loc for loc in locations if loc)),
            "department": cats.get("team") or cats.get("department") or "",
            "url": j.get("hostedUrl", ""),
        })
    return jobs


def _teamtailor_location(jobposting):
    places = jobposting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    names = []
    for place in places:
        addr = (place or {}).get("address") or {}
        name = addr.get("addressLocality") or addr.get("addressRegion") or ""
        country = addr.get("addressCountry") or ""
        if name and country in ("GB", "UK"):
            name += ", UK"
        elif name and country:
            name += f", {country}"
        elif country:
            name = country
        if name:
            names.append(name)
    if not names and jobposting.get("jobLocationType") == "TELECOMMUTE":
        names = ["Remote"]
    return "; ".join(dict.fromkeys(names))


def fetch_teamtailor(slug):
    """slug is the careers-site hostname, e.g. careers.bluelightcard.co.uk
    or {company}.teamtailor.com. Departments only exist in the RSS feed,
    locations only in the JSON feed, so both are fetched and merged."""
    base = f"https://{slug}"
    resp = requests.get(f"{base}/jobs.json", timeout=FEED_TIMEOUT,
                        headers={"User-Agent": "FAN-UK jobs board"})
    resp.raise_for_status()

    departments = {}
    try:
        rss = requests.get(f"{base}/jobs.rss", timeout=FEED_TIMEOUT,
                           headers={"User-Agent": "FAN-UK jobs board"})
        rss.raise_for_status()
        ns = "{https://teamtailor.com/locations}"
        for item in ET.fromstring(rss.content).findall(".//item"):
            guid = item.findtext("guid", "")
            departments[guid] = (item.findtext(f"{ns}department", "") or "").strip()
    except Exception:
        pass  # departments are a nice-to-have; the JSON feed remains canonical

    jobs = []
    for item in resp.json().get("items", []):
        jobs.append({
            "id": str(item["id"]),
            "title": item.get("title", ""),
            "location": _teamtailor_location(item.get("_jobposting") or {}),
            "department": departments.get(str(item["id"]), ""),
            "url": item.get("url", ""),
        })
    return jobs


def fetch_oraclecloud(slug):
    """Oracle Recruiting Cloud. slug = 'host|siteNumber|locationFacetId',
    e.g. 'eeho.fa.us2.oraclecloud.com|CX_45001|300000000106863' (the facet id
    scopes results to a country server-side; find it via the API's
    userTargetFacetInputTerm search)."""
    host, site, location_id = slug.split("|")
    jobs, offset = [], 0
    while True:
        finder = (f"findReqs;siteNumber={site},selectedLocationsFacet={location_id},"
                  f"limit=200,offset={offset},sortBy=POSTING_DATES_DESC")
        resp = requests.get(
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
            params={"onlyData": "true",
                    "expand": "requisitionList.secondaryLocations",
                    "finder": finder},
            timeout=FEED_TIMEOUT, headers={"User-Agent": "FAN-UK jobs board"})
        resp.raise_for_status()
        item = resp.json()["items"][0]
        batch = item.get("requisitionList", [])
        for r in batch:
            locations = [r.get("PrimaryLocation") or ""]
            locations += [s.get("Name", "") for s in r.get("secondaryLocations", [])]
            jobs.append({
                "id": str(r["Id"]),
                "title": r.get("Title", ""),
                "location": "; ".join(dict.fromkeys(l for l in locations if l)),
                "department": r.get("Department") or r.get("JobFamily") or "",
                "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}",
            })
        offset += len(batch)
        if not batch or offset >= int(item.get("TotalJobsCount") or 0):
            return jobs


def fetch_workday(slug):
    """Workday CXS. slug = 'host|tenant|site[|ukMode]' where ukMode is:
    - omitted: jobs are filtered to the UK client-side (global tenants
      whose country facet doesn't work, e.g. Barclays, Sky)
    - 'ALL_UK': every job on this site is UK — skip the location filter
      (UK-only employers whose multi-site roles show as 'N Locations')
    - 'facetParam=GUID': apply a server-side country facet AND treat
      results as UK (e.g. Citi Country_and_Jurisdiction=29247e57...)"""
    parts = slug.split("|")
    host, tenant, site = parts[0], parts[1], parts[2]
    uk_mode = parts[3] if len(parts) > 3 else ""
    applied, assume_uk = {}, False
    if uk_mode == "ALL_UK":
        assume_uk = True
    elif "=" in uk_mode:
        param, guid = uk_mode.split("=", 1)
        applied[param] = [guid]
        assume_uk = True

    jobs, offset, total = [], 0, None
    while True:
        resp = requests.post(
            f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
            json={"limit": 20, "offset": offset, "searchText": "",
                  "appliedFacets": applied},
            timeout=FEED_TIMEOUT,
            headers={"Accept": "application/json", "User-Agent": "FAN-UK jobs board"})
        resp.raise_for_status()
        data = resp.json()
        if total is None:  # Workday only reports total on the first page
            total = int(data.get("total") or 0)
        batch = data.get("jobPostings", [])
        for j in batch:
            path = j.get("externalPath", "")
            jobs.append({
                "id": path.rsplit("/", 1)[-1] if path else str(j.get("title", "")),
                "title": j.get("title", ""),
                "location": j.get("locationsText") or "",
                "department": "",
                "url": f"https://{host}/en-US/{site}{path}",
                "assume_uk": assume_uk,
            })
        offset += len(batch)
        if not batch or offset >= total:
            break

    # Multi-site postings only say "N Locations" in the list response; fetch
    # their detail records to resolve real locations (skip if already UK-safe).
    for job in jobs:
        if job["assume_uk"] or not re.fullmatch(r"\d+ Locations?", job["location"]):
            continue
        path = job["url"].split(f"/{site}", 1)[-1]
        try:
            detail = requests.get(
                f"https://{host}/wday/cxs/{tenant}/{site}{path}",
                timeout=FEED_TIMEOUT,
                headers={"Accept": "application/json",
                         "User-Agent": "FAN-UK jobs board"}).json()
            info = detail.get("jobPostingInfo") or {}
            locations = [info.get("location") or ""] + (info.get("additionalLocations") or [])
            resolved = "; ".join(l for l in locations if l)
            if resolved:
                job["location"] = resolved
        except Exception:
            pass  # keep "N Locations"; the UK filter will drop it conservatively
    return jobs


RADANCY_CARD_RE = re.compile(
    r'<a[^>]*href="(/job/[^"]+)"[^>]*data-job-id="(\d+)"[^>]*>(.*?)</a>'
    r'.*?<(?:p|div)[^>]*class="job-location"[^>]*>([^<]*)<', re.S)


def fetch_radancy(slug):
    """Radancy (TMP) career-search sites, e.g. search.jobs.barclays.
    slug = 'host|organizationId|ukLocationPath' where ukLocationPath is the
    GeoNames id in the site's UK-filtered URL, e.g.
    search.jobs.barclays/search-jobs/United%20Kingdom/13015/2/2635167/...
    gives slug 'search.jobs.barclays|13015|2635167'. The results endpoint
    returns JSON-wrapped HTML cards which are parsed with RADANCY_CARD_RE."""
    import html as html_mod
    host, org_id, loc_path = slug.split("|")
    jobs, seen, page, total = [], set(), 1, None
    while True:
        resp = requests.get(
            f"https://{host}/search-jobs/results",
            params={"ActiveFacetID": 0, "CurrentPage": page, "RecordsPerPage": 50,
                    "Distance": 50, "RadiusUnitType": 0, "Keywords": "",
                    "Location": "United Kingdom", "LocationType": 2,
                    "LocationPath": loc_path, "OrganizationIds": org_id,
                    "ShowRadius": "False", "IsPagination": "True",
                    "SearchResultsModuleName": "Search Results",
                    "SearchFiltersModuleName": "Search Filters",
                    "SortCriteria": 0, "SortDirection": 0, "SearchType": 5},
            timeout=FEED_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (FAN-UK jobs board)"})
        resp.raise_for_status()
        fragment = resp.json().get("results", "")
        if total is None:
            m = re.search(r'data-total-results="?(\d+)', fragment)
            total = int(m.group(1)) if m else 0
        batch = RADANCY_CARD_RE.findall(fragment)
        for path, job_id, title_html, loc in batch:
            if job_id in seen:
                continue
            seen.add(job_id)
            title = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", "", title_html)).split())
            jobs.append({
                "id": job_id,
                "title": title,
                "location": " ".join(html_mod.unescape(loc).split()),
                "department": "",
                "url": f"https://{host}{path}",
            })
        if not batch or len(jobs) >= total:
            return jobs
        page += 1
        time.sleep(0.3)  # the endpoint is rate-limited (150 req / 15s window)


ADAPTERS = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby,
            "lever": fetch_lever, "teamtailor": fetch_teamtailor,
            "oraclecloud": fetch_oraclecloud, "workday": fetch_workday,
            "radancy": fetch_radancy}


# --- filters ------------------------------------------------------------------

def is_uk(location, cfg):
    loc = location.lower()
    if any(s in loc for s in cfg["filters"]["uk_location_substrings"]):
        return True
    return bool(UK_WORD_RE.search(location))


def is_tech(job, cfg):
    title = job["title"].lower()
    if any(re.search(rf"\b{re.escape(k)}\b", title)
           for k in cfg["filters"].get("exclude_title_keywords", [])):
        return False
    haystack = f"{job['title']} {job['department']}".lower()
    return any(re.search(rf"\b{re.escape(k)}\b", haystack)
               for k in cfg["filters"]["tech_keywords"])


def filtered_jobs(raw_jobs, cfg):
    return [j for j in raw_jobs
            if (j.get("assume_uk") or is_uk(j["location"], cfg)) and is_tech(j, cfg)]


def classify_discipline(job, cfg):
    haystack = f"{job['title']} {job['department']}".lower()
    for discipline, keywords in cfg["disciplines"].items():
        if any(re.search(rf"\b{re.escape(str(k))}\b", haystack) for k in keywords):
            return discipline
    return "Other"


NON_LONDON_PLACES = None  # cached from config on first use


def location_tags(location, cfg):
    global NON_LONDON_PLACES
    if NON_LONDON_PLACES is None:
        NON_LONDON_PLACES = [s for s in cfg["filters"]["uk_location_substrings"]
                             if s not in ("london", "united kingdom", "england")]
    loc = location.lower()
    tags = []
    if "london" in loc:
        tags.append("London")
    if any(p in loc for p in NON_LONDON_PLACES):
        tags.append("Outside London")
    if "remote" in loc or "hybrid" in loc:
        tags.append("Remote-friendly")
    if not any(t in tags for t in ("London", "Outside London")):
        tags.append("UK (no city listed)")
    return tags


# --- Notion sync ---------------------------------------------------------------

def read_companies(cfg):
    """Companies from Notion: [{name, ats, slug, active, sponsor, page_id}]."""
    companies = []
    for page in notion.query_database(cfg["notion"]["companies_database_id"]):
        props = page["properties"]
        companies.append({
            "page_id": page["id"],
            "name": notion.plain_text(props["Name"]),
            "ats": notion.select_value(props["ATS"]),
            "slug": notion.plain_text(props["Slug"]),
            "active": props["Active"]["checkbox"],
            "sponsor": props["Licensed sponsor"]["checkbox"],
        })
    return companies


def read_existing_jobs(cfg):
    """Existing job pages keyed by Source ID."""
    existing = {}
    for page in notion.query_database(cfg["notion"]["jobs_database_id"]):
        props = page["properties"]
        source_id = notion.plain_text(props["Source ID"])
        if source_id:
            existing[source_id] = {
                "page_id": page["id"],
                "status": notion.select_value(props["Status"]),
                "sponsor": props["Licensed sponsor"]["checkbox"],
                "discipline": notion.select_value(props["Discipline"]),
                "location_tags": sorted(t["name"] for t in
                                        props["Location tags"]["multi_select"]),
            }
    return existing


def job_properties(job, company, today, cfg):
    return {
        "Role": notion.title(job["title"]),
        "Company": notion.select(company["name"]),
        "Location": notion.rich_text(job["location"]),
        "Apply": notion.url(job["url"]),
        "Department": notion.rich_text(job["department"]),
        "Licensed sponsor": notion.checkbox(company["sponsor"]),
        "First seen": notion.date(today),
        "Status": notion.select("Open"),
        "Source ID": notion.rich_text(job["source_id"]),
        "Discipline": notion.select(classify_discipline(job, cfg)),
        "Location tags": notion.multi_select(location_tags(job["location"], cfg)),
    }


def sync_company(company, jobs, existing, cfg, today, stats):
    jobs_db = cfg["notion"]["jobs_database_id"]
    prefix = f"{company['ats']}:{company['slug']}:"
    feed_ids = set()

    for job in jobs:
        job["source_id"] = f"{prefix}{job['id']}"
        feed_ids.add(job["source_id"])
        current = existing.get(job["source_id"])
        if current is None:
            notion.create_page(jobs_db, job_properties(job, company, today, cfg))
            stats["created"] += 1
        else:
            updates = {}
            if current["status"] != "Open":
                updates["Status"] = notion.select("Open")
                updates["Closed on"] = notion.date(None)
                stats["reopened"] += 1
            if current["sponsor"] != company["sponsor"]:
                updates["Licensed sponsor"] = notion.checkbox(company["sponsor"])
            discipline = classify_discipline(job, cfg)
            if current["discipline"] != discipline:
                updates["Discipline"] = notion.select(discipline)
            tags = location_tags(job["location"], cfg)
            if current["location_tags"] != sorted(tags):
                updates["Location tags"] = notion.multi_select(tags)
            if updates:
                notion.update_page(current["page_id"], updates)

    for source_id, current in existing.items():
        if (source_id.startswith(prefix) and source_id not in feed_ids
                and current["status"] == "Open"):
            notion.update_page(current["page_id"],
                               {"Status": notion.select("Closed"),
                                "Closed on": notion.date(today)})
            stats["closed"] += 1


def close_all_for(company, existing, stats, today):
    prefix = f"{company['ats']}:{company['slug']}:"
    for source_id, current in existing.items():
        if source_id.startswith(prefix) and current["status"] == "Open":
            notion.update_page(current["page_id"],
                               {"Status": notion.select("Closed"),
                                "Closed on": notion.date(today)})
            stats["closed"] += 1


# --- entry points ---------------------------------------------------------------

def dry_run(cfg):
    print("DRY RUN — fetching feeds and applying filters, no Notion access\n")
    total = 0
    for company in cfg["dry_run_companies"]:
        try:
            raw = ADAPTERS[company["ats"]](company["slug"])
        except Exception as exc:
            print(f"  {company['name']}: FEED ERROR — {exc}")
            continue
        kept = filtered_jobs(raw, cfg)
        total += len(kept)
        print(f"  {company['name']}: {len(raw)} in feed -> {len(kept)} kept (UK + tech)")
        for job in kept[:3]:
            print(f"      · {job['title']} — {job['location']}")
    print(f"\nTotal roles that would be on the board: {total}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and filter feeds without touching Notion")
    args = parser.parse_args()

    cfg = load_config()
    if args.dry_run:
        dry_run(cfg)
        return

    today = datetime.date.today().isoformat()
    companies = read_companies(cfg)
    existing = read_existing_jobs(cfg)
    stats = {"created": 0, "reopened": 0, "closed": 0, "archived": 0}
    failures = []

    for company in companies:
        if not company["active"]:
            close_all_for(company, existing, stats, today)
            continue
        if company["ats"] not in ADAPTERS or not company["slug"]:
            print(f"WARNING: {company['name']} skipped — missing/unknown ATS or slug")
            failures.append(company["name"])
            continue
        try:
            raw = ADAPTERS[company["ats"]](company["slug"])
        except Exception as exc:
            print(f"WARNING: {company['name']} feed failed, jobs left untouched: {exc}")
            failures.append(company["name"])
            continue
        kept = filtered_jobs(raw, cfg)
        print(f"{company['name']}: {len(raw)} in feed -> {len(kept)} kept")
        sync_company(company, kept, existing, cfg, today, stats)

    # Orphans: jobs whose company row was deleted from the Companies DB.
    # Archived (removed) rather than closed — a deleted company has no
    # reopen path, and closed leftovers clutter grouped views.
    known_prefixes = {f"{c['ats']}:{c['slug']}:" for c in companies
                      if c["ats"] and c["slug"]}
    for source_id, current in existing.items():
        if not any(source_id.startswith(p) for p in known_prefixes):
            notion.archive_page(current["page_id"])
            stats["archived"] += 1

    print(f"\nDone. Created {stats['created']}, reopened {stats['reopened']}, "
          f"closed {stats['closed']}, archived {stats['archived']} (orphans). "
          f"Feed failures: {failures or 'none'}")
    if failures and len(failures) == len([c for c in companies if c["active"]]):
        sys.exit("All feeds failed — treating run as an error")

    import export
    export.export(cfg)


if __name__ == "__main__":
    main()
