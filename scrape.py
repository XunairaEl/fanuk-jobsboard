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


ADAPTERS = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby, "lever": fetch_lever}


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
    return [j for j in raw_jobs if is_uk(j["location"], cfg) and is_tech(j, cfg)]


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
            }
    return existing


def job_properties(job, company, today):
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
            notion.create_page(jobs_db, job_properties(job, company, today))
            stats["created"] += 1
        else:
            updates = {}
            if current["status"] != "Open":
                updates["Status"] = notion.select("Open")
                stats["reopened"] += 1
            if current["sponsor"] != company["sponsor"]:
                updates["Licensed sponsor"] = notion.checkbox(company["sponsor"])
            if updates:
                notion.update_page(current["page_id"], updates)

    for source_id, current in existing.items():
        if (source_id.startswith(prefix) and source_id not in feed_ids
                and current["status"] == "Open"):
            notion.update_page(current["page_id"], {"Status": notion.select("Closed")})
            stats["closed"] += 1


def close_all_for(company, existing, stats):
    prefix = f"{company['ats']}:{company['slug']}:"
    for source_id, current in existing.items():
        if source_id.startswith(prefix) and current["status"] == "Open":
            notion.update_page(current["page_id"], {"Status": notion.select("Closed")})
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
    stats = {"created": 0, "reopened": 0, "closed": 0}
    failures = []

    for company in companies:
        if not company["active"]:
            close_all_for(company, existing, stats)
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

    print(f"\nDone. Created {stats['created']}, reopened {stats['reopened']}, "
          f"closed {stats['closed']}. Feed failures: {failures or 'none'}")
    if failures and len(failures) == len([c for c in companies if c["active"]]):
        sys.exit("All feeds failed — treating run as an error")


if __name__ == "__main__":
    main()
