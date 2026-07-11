# FAN-UK Jobs Board

**Member-facing board: https://xunairael.github.io/fanuk-jobsboard/** — a static
filterable page (`docs/`) fed by `docs/jobs.json`, republished daily.

The scraper runs daily on GitHub Actions: reads the **Companies** database in Notion,
fetches each company's public careers feed (Greenhouse / Ashby / Lever / Teamtailor /
Oracle Recruiting Cloud / Workday / Radancy), filters to **UK-based tech/CS roles**,
syncs the Notion **Jobs** database, then exports open roles from Notion to
`docs/jobs.json` (`export.py`). Roles that disappear from a feed are marked Closed.
Weekly, the Home Office Register of Licensed Sponsors is re-downloaded to refresh each
company's **Licensed sponsor** flag. The Notion page itself stays unpublished — it is
the data store and admin UI, not the member experience.

Adding/removing companies needs **no code changes** — admins edit the Companies
database in Notion (see the "Admin — Companies & How it works" page there).

## One-time setup

1. **Notion integration**: at [notion.so/my-integrations](https://www.notion.so/my-integrations)
   create an internal integration (e.g. "FAN-UK jobs scraper"), copy its secret token.
   Then on the *FAN-UK Jobs Board* page in Notion: ••• menu → Connections → add the
   integration (this grants it access to the page and both databases under it).
2. **GitHub**: push this folder to a repo, then in repo Settings → Secrets and
   variables → Actions add a secret named `NOTION_TOKEN` with the integration token.
3. Done — the workflows in `.github/workflows/` run on their own (daily scrape 06:00 UTC,
   weekly sponsor refresh Monday 05:30 UTC). Both can also be run manually from the
   Actions tab. GitHub emails the repo owner if a run fails.

## Running locally

```bash
pip install -r requirements.txt
python scrape.py --dry-run          # fetch + filter only, no Notion needed
NOTION_TOKEN=secret_xxx python scrape.py     # full sync
NOTION_TOKEN=secret_xxx python sponsors.py   # sponsor refresh
python sponsors.py --dry-run                 # needs NOTION_TOKEN too (reads companies)
```

## Config

`config.yaml` holds the Notion database IDs and the filter rules (UK location
terms, tech keyword allowlist). Tune keywords there; the company list itself
lives in Notion, not here.

## Design notes

- Jobs are deduplicated on `Source ID` = `{ats}:{slug}:{job_id}`.
- A feed that errors is skipped and its existing jobs left untouched — a
  transient outage never mass-closes listings. The run only fails outright if
  *every* active feed fails.
- The sponsor flag means the company is on the Home Office register (Skilled
  Worker route) — it is **not** a promise that a specific role is sponsored.
- Notion writes are throttled to ~3 requests/second (Notion's rate limit).
