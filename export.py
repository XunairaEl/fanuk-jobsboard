"""Export open roles from Notion to docs/jobs.json for the public board page.

Reads from Notion (the source of truth) rather than the raw feeds so a
transient feed failure never empties a company's roles from the public page.
Company size is joined from the Companies database by name; a rename that
breaks the join logs a warning and the affected roles keep an empty size
(they still appear on the board under "All").

Usage:
    NOTION_TOKEN=... python export.py [output_path]
Also invoked automatically at the end of scrape.py runs.
"""

import datetime
import json
import sys

import yaml

import notion_api as notion


def export(cfg, out_path="docs/jobs.json"):
    sizes = {}
    for page in notion.query_database(cfg["notion"]["companies_database_id"]):
        props = page["properties"]
        sizes[notion.plain_text(props["Name"])] = notion.select_value(props["Size"]) or ""

    jobs = []
    for page in notion.query_database(
            cfg["notion"]["jobs_database_id"],
            {"property": "Status", "select": {"equals": "Open"}}):
        props = page["properties"]
        company = notion.select_value(props["Company"]) or ""
        if company not in sizes:
            print(f"WARNING: company '{company}' not found in Companies DB — "
                  f"size join broken (renamed?); role kept with empty size")
        first_seen = (props["First seen"]["date"] or {}).get("start", "")
        jobs.append({
            "title": notion.plain_text(props["Role"]),
            "company": company,
            "size": sizes.get(company, ""),
            "location": notion.plain_text(props["Location"]),
            "tags": [t["name"] for t in props["Location tags"]["multi_select"]],
            "discipline": notion.select_value(props["Discipline"]) or "Other",
            "sponsor": props["Licensed sponsor"]["checkbox"],
            "first_seen": first_seen,
            "apply": props["Apply"]["url"] or "",
        })

    jobs.sort(key=lambda j: j["first_seen"], reverse=True)
    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs": jobs,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Exported {len(jobs)} open roles to {out_path}")


if __name__ == "__main__":
    with open("config.yaml") as f:
        config = yaml.safe_load(f)
    export(config, sys.argv[1] if len(sys.argv) > 1 else "docs/jobs.json")
