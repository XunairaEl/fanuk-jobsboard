"""Report jobs-board statistics from the Notion Jobs database.

Prints a Markdown summary suitable for pasting into board updates:
open roles by company, roles surfaced per month, closure stats
(median days open), and sponsor share.

Usage:
    NOTION_TOKEN=... python stats.py
Also runnable from the GitHub Actions tab ("Jobs board stats" workflow) —
the report appears in the run log.
"""

import datetime
import statistics

import yaml

import notion_api as notion


def load_jobs(cfg):
    jobs = []
    for page in notion.query_database(cfg["notion"]["jobs_database_id"]):
        props = page["properties"]
        first_seen = (props["First seen"]["date"] or {}).get("start")
        closed_on = (props["Closed on"]["date"] or {}).get("start")
        jobs.append({
            "company": notion.select_value(props["Company"]) or "?",
            "status": notion.select_value(props["Status"]),
            "sponsor": props["Licensed sponsor"]["checkbox"],
            "first_seen": datetime.date.fromisoformat(first_seen) if first_seen else None,
            "closed_on": datetime.date.fromisoformat(closed_on) if closed_on else None,
        })
    return jobs


def main():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    jobs = load_jobs(cfg)
    today = datetime.date.today()
    open_jobs = [j for j in jobs if j["status"] == "Open"]
    closed_jobs = [j for j in jobs if j["status"] == "Closed"]

    print(f"# FAN-UK Jobs Board — stats as of {today.isoformat()}\n")
    print(f"- **Open roles right now:** {len(open_jobs)}")
    print(f"- **Roles surfaced all-time:** {len(jobs)} "
          f"(since {min((j['first_seen'] for j in jobs if j['first_seen']), default=today)})")
    sponsor_open = sum(1 for j in open_jobs if j["sponsor"])
    if open_jobs:
        print(f"- **Open roles at licensed sponsors:** {sponsor_open} "
              f"({100 * sponsor_open // len(open_jobs)}%)")

    print("\n## Open roles by company\n")
    by_company = {}
    for j in open_jobs:
        by_company[j["company"]] = by_company.get(j["company"], 0) + 1
    for company, n in sorted(by_company.items(), key=lambda kv: -kv[1]):
        print(f"- {company}: {n}")

    print("\n## New roles surfaced per month\n")
    by_month = {}
    for j in jobs:
        if j["first_seen"]:
            key = j["first_seen"].strftime("%Y-%m")
            by_month[key] = by_month.get(key, 0) + 1
    for month in sorted(by_month):
        print(f"- {month}: {by_month[month]}")

    durations = [(j["closed_on"] - j["first_seen"]).days
                 for j in closed_jobs if j["closed_on"] and j["first_seen"]]
    print(f"\n## Closures\n")
    print(f"- Roles closed all-time: {len(closed_jobs)}")
    if durations:
        print(f"- Median days a role stayed on the board: "
              f"{statistics.median(durations):.0f}")
        print(f"- Fastest closure: {min(durations)} days · slowest: {max(durations)} days")
    else:
        print("- No closure-duration data yet.")

    print("\n_Time-on-board counts from when the board first saw a role, "
          "not when the company posted it. Roles present at launch "
          "(2026-07-10) carry that as their first-seen date._")


if __name__ == "__main__":
    main()
