"""Refresh the Licensed sponsor flag on the Notion Companies database.

Downloads the UK Home Office Register of Licensed Sponsors (Workers) —
a public CSV republished frequently on gov.uk — and matches each company:

- If "Register match name" is set on the company row, an exact
  (case-insensitive) match against that organisation name is required.
- Otherwise the company Name is matched as a whole word within
  organisation names, and the first hit is recorded in
  "Register match name" for admins to review.

Usage:
    python sponsors.py           # needs NOTION_TOKEN
    python sponsors.py --dry-run # report matches without updating Notion
"""

import argparse
import csv
import io
import re

import requests
import yaml

import notion_api as notion


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def download_register(cfg):
    """Resolve the current dated CSV link from the gov.uk publication page."""
    page = requests.get(cfg["sponsor_register"]["publication_url"], timeout=60,
                        headers={"User-Agent": "FAN-UK jobs board (community, non-commercial)"})
    page.raise_for_status()
    links = re.findall(r'https://[^"\']+\.csv', page.text)
    if not links:
        raise RuntimeError("No CSV link found on the sponsor register publication page")
    resp = requests.get(links[0], timeout=300)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))
    orgs = [(row.get("Organisation Name") or "").strip() for row in reader]
    orgs = [o for o in orgs if o]
    print(f"Register downloaded: {len(orgs)} organisation rows ({links[0]})")
    return orgs


def match(company_name, pinned_name, orgs):
    """Return the matched organisation name, or None."""
    if pinned_name:
        pinned_lower = pinned_name.lower()
        for org in orgs:
            if org.lower() == pinned_lower:
                return org
        return None
    word_re = re.compile(rf"\b{re.escape(company_name)}\b", re.IGNORECASE)
    for org in orgs:
        if word_re.search(org):
            return org
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    orgs = download_register(cfg)

    for page in notion.query_database(cfg["notion"]["companies_database_id"]):
        props = page["properties"]
        name = notion.plain_text(props["Name"])
        pinned = notion.plain_text(props["Register match name"])
        currently = props["Licensed sponsor"]["checkbox"]

        matched = match(name, pinned, orgs)
        is_sponsor = matched is not None
        print(f"{name}: {'sponsor — ' + matched if is_sponsor else 'no register match'}")

        if args.dry_run:
            continue
        updates = {}
        if is_sponsor != currently:
            updates["Licensed sponsor"] = notion.checkbox(is_sponsor)
        if matched and not pinned:
            updates["Register match name"] = notion.rich_text(matched)
        if updates:
            notion.update_page(page["id"], updates)

    # Job rows inherit the flag on the next daily scrape (scrape.py syncs
    # sponsor status of existing open jobs), so nothing more to do here.


if __name__ == "__main__":
    main()
