#!/usr/bin/env python3
"""Pushes today's logs/matches.csv into Supabase (mandate_opportunities table)
and uploads each matched tender's downloaded PDFs to the mandate-documents
storage bucket. Meant to run right after mandate_bot.main, using the same
matches.csv it just wrote — safe to re-run, everything is upserted."""

from __future__ import annotations

import csv
import mimetypes
import os
import sys

import requests
from dateutil import parser as dateparser

ROOT = os.path.dirname(os.path.abspath(__file__))
MATCHES_CSV = os.path.join(ROOT, "logs", "matches.csv")
DATE_REFRESH_CSV = os.path.join(ROOT, "logs", "date_refresh.csv")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

HEADERS = {
    "apikey": SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
}


def dedupe_key(row: dict) -> str:
    """Mirrors Tender.key in mandate_bot/models.py so a row upserts onto the
    same identity the bot itself already uses for de-duplication. A source
    that pinned its own key (Tender.dedupe_override) writes it into the CSV;
    older rows predate the column and fall through to the same URL/ref
    preference as before."""
    if row.get("dedupe_key"):
        return row["dedupe_key"]
    if row["document_url"]:
        return row["document_url"]
    if row["notice_url"]:
        return row["notice_url"]
    if row["tender_ref"]:
        return f"{row['source']}:{row['tender_ref']}"
    return f"{row['title']}|{row['department']}|{row['publish_date']}"


def parse_date(value: str) -> str | None:
    """Portals write dates in whatever format they like, so an unparseable one
    is a bad cell rather than a reason to fail the whole sync."""
    if not value:
        return None
    try:
        parsed = dateparser.parse(value, dayfirst=True)
    except (ValueError, OverflowError, TypeError):
        parsed = None
    if parsed is None:
        print(f"WARN: could not parse date {value!r} — leaving it empty", file=sys.stderr)
        return None
    return parsed.date().isoformat()


def load_rows() -> list[dict]:
    if not os.path.exists(MATCHES_CSV):
        return []
    with open(MATCHES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def upsert_opportunities(rows: list[dict]) -> None:
    if not rows:
        print("No matches to sync.")
        return

    # matches.csv can carry literal duplicate rows for the same tender
    # (e.g. re-matched across separate local runs). A single upsert batch
    # can't hit the same on_conflict key twice, so collapse by dedupe_key
    # before sending — last occurrence wins.
    by_key: dict[str, dict] = {}
    for row in rows:
        by_key[dedupe_key(row)] = row

    payload = [
        {
            "dedupe_key": key,
            "source": row["source"],
            "title": row["title"],
            "department": row["department"] or None,
            "category": row["category"] or None,
            "notice_type": row["notice_type"] or None,
            "publish_date": parse_date(row["publish_date"]),
            "close_date": parse_date(row["close_date"]),
            "matched_keywords": [k.strip() for k in row["matched_keywords"].split(";") if k.strip()],
            "notice_url": row["notice_url"] or None,
            "document_url": row["document_url"] or None,
            "extra_urls": [u.strip() for u in row["extra_urls"].split(";") if u.strip()],
            "tender_ref": row["tender_ref"] or None,
            "storage_folder": os.path.basename(row["saved_dir"].rstrip("/")) if row["saved_dir"] else None,
            "found_at": row["found_at"],
        }
        for key, row in by_key.items()
    ]

    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/mandate_opportunities",
        headers={
            **HEADERS,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": "dedupe_key"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    print(f"Upserted {len(payload)} opportunity row(s).")


def upload_documents(rows: list[dict]) -> None:
    uploaded = 0
    for row in rows:
        saved_dir = row.get("saved_dir")
        if not saved_dir:
            continue
        local_dir = saved_dir if os.path.isabs(saved_dir) else os.path.join(ROOT, saved_dir)
        if not os.path.isdir(local_dir):
            continue

        folder_name = os.path.basename(saved_dir.rstrip("/"))
        for fname in sorted(os.listdir(local_dir)):
            fpath = os.path.join(local_dir, fname)
            if not os.path.isfile(fpath):
                continue

            storage_path = f"{folder_name}/{fname}"
            content_type = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            with open(fpath, "rb") as f:
                data = f.read()

            resp = requests.post(
                f"{SUPABASE_URL}/storage/v1/object/mandate-documents/{storage_path}",
                headers={**HEADERS, "Content-Type": content_type, "x-upsert": "true"},
                data=data,
                timeout=120,
            )
            if resp.status_code >= 300:
                print(f"WARN: failed to upload {storage_path}: {resp.status_code} {resp.text}", file=sys.stderr)
                continue
            uploaded += 1

    print(f"Uploaded {uploaded} document(s).")


def refresh_dates() -> None:
    """Patches publish/close dates onto opportunities that are already in the
    table. A source writes date_refresh.csv for everything it can currently
    see, matched or not, because dates move after first sighting — ADB extends
    an advertisement's deadline and the CSRN's Publishing History gains an
    Extension row. Only the two date columns are touched, so a row's documents,
    keywords and storage folder are left exactly as they are."""
    if not os.path.exists(DATE_REFRESH_CSV):
        return
    with open(DATE_REFRESH_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    patched = 0
    for row in rows:
        key = row.get("dedupe_key")
        if not key:
            continue
        payload = {
            "publish_date": parse_date(row.get("publish_date", "")),
            "close_date": parse_date(row.get("close_date", "")),
        }
        if payload["publish_date"] is None and payload["close_date"] is None:
            continue

        resp = requests.patch(
            f"{SUPABASE_URL}/rest/v1/mandate_opportunities",
            headers={**HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"},
            params={"dedupe_key": f"eq.{key}"},
            json=payload,
            timeout=60,
        )
        if resp.status_code >= 300:
            print(f"WARN: failed to refresh dates for {key}: {resp.status_code} {resp.text}", file=sys.stderr)
            continue
        patched += 1

    print(f"Refreshed dates on {patched} existing opportunity row(s).")


def main():
    rows = load_rows()
    print(f"Loaded {len(rows)} row(s) from matches.csv")
    upsert_opportunities(rows)
    upload_documents(rows)
    refresh_dates()


if __name__ == "__main__":
    main()
