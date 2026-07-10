"""Minimal Notion REST API client for the FAN-UK jobs board scraper."""

import os
import time

import requests

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
# Notion allows ~3 requests/second; stay politely under it.
WRITE_DELAY_SECONDS = 0.35


def _headers():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise SystemExit("NOTION_TOKEN environment variable is not set")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def request(method, path, payload=None, retries=3):
    for attempt in range(retries + 1):
        resp = requests.request(
            method, f"{API_BASE}{path}", headers=_headers(), json=payload, timeout=30
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < retries:
                wait = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                time.sleep(wait)
                continue
        if not resp.ok:
            raise RuntimeError(f"Notion API {method} {path} failed "
                               f"({resp.status_code}): {resp.text[:300]}")
        return resp.json()
    raise RuntimeError(f"Notion API {method} {path}: retries exhausted")


def query_database(database_id, filter_payload=None):
    """Return all pages in a database, following pagination."""
    pages, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        if filter_payload:
            payload["filter"] = filter_payload
        data = request("POST", f"/databases/{database_id}/query", payload)
        pages.extend(data["results"])
        if not data.get("has_more"):
            return pages
        cursor = data["next_cursor"]


def create_page(database_id, properties):
    time.sleep(WRITE_DELAY_SECONDS)
    return request("POST", "/pages",
                   {"parent": {"database_id": database_id}, "properties": properties})


def update_page(page_id, properties):
    time.sleep(WRITE_DELAY_SECONDS)
    return request("PATCH", f"/pages/{page_id}", {"properties": properties})


def archive_page(page_id):
    time.sleep(WRITE_DELAY_SECONDS)
    return request("PATCH", f"/pages/{page_id}", {"archived": True})


# --- property value builders -------------------------------------------------

def title(text):
    return {"title": [{"text": {"content": text[:2000]}}]}


def rich_text(text):
    return {"rich_text": [{"text": {"content": text[:2000]}}]} if text else {"rich_text": []}


def url(value):
    return {"url": value or None}


def select(name):
    return {"select": {"name": name.replace(",", " ")[:100]}}


def checkbox(value):
    return {"checkbox": bool(value)}


def date(iso_date):
    return {"date": {"start": iso_date}}


# --- property value readers --------------------------------------------------

def plain_text(prop):
    """Extract plain text from a title or rich_text property value."""
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def select_value(prop):
    sel = prop.get("select")
    return sel["name"] if sel else None
