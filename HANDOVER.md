# FAN-UK Jobs Board — Handover Notes

_For future admins and for the members-portal developer. Last updated 2026-07-10._

## What this is

An automated jobs board for the FAN-UK community. No one posts jobs manually:
a scheduled scraper reads the public careers feeds of a curated company list and
keeps a Notion database in sync. Members view a published Notion page.

## The moving parts

| Part | Where | Who controls it |
|---|---|---|
| Jobs board page + databases | Notion, "FAN-UK Jobs Board" page (Zunaira's account) | Admins edit the **Companies** DB only |
| Scraper code | github.com/XunairaEl/fanuk-jobsboard | Developers |
| Daily scrape (06:00 UTC) + weekly sponsor refresh (Mon 05:30 UTC) | GitHub Actions on that repo | Automatic; manual re-run from the Actions tab |
| Auth | `NOTION_TOKEN` repo secret → Notion internal integration connected to the page | Zunaira |

## Routine admin (no code)

Everything routine happens in the Notion **Companies** database — see the
"Admin — Companies & How it works" page in Notion for add/pause instructions.

## If something breaks

- **GitHub emails the repo owner when a run fails.** Open the Actions tab → the
  failed run → read the log. A single company failing does NOT fail the run;
  its jobs are simply left untouched (look for `WARNING:` lines).
- A company's jobs suddenly all Closed usually means the company changed ATS
  or slug — re-verify their careers feed and update the row.
- Filter tuning (roles wrongly included/excluded): edit `config.yaml`
  (`tech_keywords`, `exclude_title_keywords`, `uk_location_substrings`).

## Handover / succession

- Notion: transfer or duplicate the "FAN-UK Jobs Board" page to a FAN-UK
  workspace when one exists; recreate the integration + token there and update
  the `NOTION_TOKEN` secret.
- GitHub: transfer the repo (Settings → Danger Zone → Transfer) to a FAN-UK org
  account when one exists. Secrets do not transfer — re-add `NOTION_TOKEN`.

## For the members-portal developer (Vercel + Supabase)

When the portal's job feature is ready, absorb this board rather than migrate it:

1. Reuse `scrape.py`'s adapters and filters; replace the Notion sync layer
   (`notion_api.py`) with Supabase writes. The pipeline re-derives all job data
   from the feeds, so there is no historical data to migrate.
2. Carry over the **Companies** table contents (8–N rows: name, ats, slug,
   register match name) — that's the only real state.
3. `sponsors.py` is portable as-is apart from the same sync layer; the Home
   Office register download + matching logic doesn't touch Notion until the end.
4. Then retire the Notion page (or keep it as a read-only mirror during transition).

## GDPR note

The system stores no member personal data — only public job advertisements and
the public sponsor register. The Notion integration token grants access solely
to the jobs board page.
