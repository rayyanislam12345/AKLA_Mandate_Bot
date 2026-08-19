"""Scraper for the World Bank's Procurement Notices search
(projects.worldbank.org/en/projects-operations/procurement), filtered to
Pakistan. Backed directly by search.worldbank.org's own JSON API
(api/v2/procnotices) rather than the Angular front-end that page presents.

Two things make this the lightest source yet:

1. The site's own "Country" URL query param (`countrycode_exact=PK`, as
   given by the user) does NOT filter server-side at all — confirmed via
   curl (unfiltered `total: 415389`), via the live browser's own captured
   network request/response, and via a full-page screenshot of the live
   site showing unfiltered global results despite the URL param
   (2026-08-19). The field that actually works is
   `project_ctry_name_exact=Pakistan` (`total: 10020`), found by
   inspecting the API's own facet field list.
2. The API's default (unrestricted `fl=`) response already includes a
   `notice_text` field containing the notice's full content as an HTML
   fragment — the same content that renders on the Angular detail page.
   Confirmed populated for every notice type kept below (Invitation for
   Bids, Request for Expression of Interest, General Procurement Notice,
   Invitation for Prequalification). No separate document/attachment
   field exists anywhere in the schema (checked full unrestricted records
   for each type) — notice_text IS the document here. That means listing
   AND keyword-matching can both be done straight from this one API call,
   no per-candidate page rendering needed at all. Playwright is only used,
   per CONFIRMED MATCH, to turn notice_text into a PDF snapshot for the
   downloads folder — page.set_content() + page.pdf() on the
   already-fetched HTML, no network navigation involved.

"Contract Award" notices (announcing an already-decided winner, not an
open opportunity) are excluded — confirmed via the API's own
notice_type_exact facet counts for Pakistan (2026-08-19): Contract Award
5893, Invitation for Bids 2021, Request for Expression of Interest 1989,
General Procurement Notice 70, Invitation for Prequalification 47 — this
is the complete type list for Pakistan as of that date.
"""
from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

from .http_utils import get_with_retry
from .logging_utils import append_match_log, unique_dest_dir
from .matcher import find_matches
from .models import Tender

log = logging.getLogger("mandate_bot.worldbank")

API_URL = "https://search.worldbank.org/api/v2/procnotices"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MandateBot/1.0"}

NOTICE_TYPES = [
    "Invitation for Bids",
    "Request for Expression of Interest",
    "General Procurement Notice",
    "Invitation for Prequalification",
]

TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(raw_html: str) -> str:
    return html.unescape(TAG_RE.sub(" ", raw_html or ""))


def _parse_record(rec: dict) -> Tender:
    notice_id = rec.get("id", "")
    return Tender(
        notice_type=rec.get("notice_type", ""),
        title=rec.get("bid_description") or rec.get("project_name") or "(untitled)",
        category=rec.get("project_name", ""),
        publish_date=rec.get("noticedate", ""),
        close_date=(rec.get("submission_deadline_date") or "")[:10],
        department=rec.get("contact_organization") or rec.get("contact_name", ""),
        status=rec.get("notice_status", ""),
        notice_url=f"https://projects.worldbank.org/en/projects-operations/procurement-detail/{notice_id}" if notice_id else None,
        document_url=None,
        source="worldbank",
        tender_ref=rec.get("bid_reference_no") or notice_id,
        raw_content=rec.get("notice_text", ""),
    )


def fetch_all(request_delay: float = 0.5, page_size: int = 100) -> list[Tender]:
    session = requests.Session()
    session.headers.update(HEADERS)

    tenders: list[Tender] = []
    for notice_type in NOTICE_TYPES:
        offset = 0
        while True:
            params = {
                "format": "json",
                "rows": page_size,
                "os": offset,
                "project_ctry_name_exact": "Pakistan",
                "notice_type_exact": notice_type,
            }
            log.info("Fetching %s, offset %d", notice_type, offset)
            resp = get_with_retry(session, API_URL, log, params=params, timeout=30)
            data = resp.json()
            records = data.get("procnotices", [])
            if not records:
                break
            tenders.extend(_parse_record(r) for r in records)
            total = int(data.get("total", 0))
            offset += len(records)
            if offset >= total:
                break
            time.sleep(request_delay)

    log.info("World Bank: %d tenders fetched total (Pakistan, excluding Contract Award)", len(tenders))
    return tenders


def process_candidates(candidates: list[Tender], keywords: list[str], cfg: dict, state, log_: logging.Logger) -> int:
    match_count = 0
    pw_ctx = None
    browser = None

    try:
        for t in candidates:
            hits = find_matches(_html_to_text(t.raw_content), keywords)
            if not hits:
                state.mark(t.key)
                continue

            try:
                if browser is None:
                    pw_ctx = sync_playwright().start()
                    browser = pw_ctx.chromium.launch()

                dest_dir = unique_dest_dir(cfg["paths"]["download_dir"], t.source, t.tender_ref or t.title)
                os.makedirs(dest_dir, exist_ok=True)

                try:
                    page = browser.new_page()
                    page.set_content(f"<html><body>{t.raw_content}</body></html>")
                    page.pdf(path=os.path.join(dest_dir, "notice.pdf"))
                    page.close()
                except Exception:
                    log_.warning("Failed to render PDF snapshot for %r, saving raw HTML instead", t.title[:80])
                    with open(os.path.join(dest_dir, "notice.html"), "w", encoding="utf-8") as f:
                        f.write(t.raw_content)

                match_count += 1
                log_.info("MATCH: %s (%s) — keywords: %s", t.title, t.department, hits)
                append_match_log(cfg["paths"]["match_log"], {
                    "found_at": datetime.now().isoformat(timespec="seconds"),
                    "source": t.source,
                    "title": t.title,
                    "department": t.department,
                    "category": t.category,
                    "notice_type": t.notice_type,
                    "publish_date": t.publish_date,
                    "close_date": t.close_date,
                    "matched_keywords": "; ".join(hits),
                    "notice_url": t.notice_url or "",
                    "document_url": t.document_url or "",
                    "saved_dir": dest_dir,
                    "tender_ref": t.tender_ref,
                    "extra_urls": "",
                })
                state.mark(t.key)
            except Exception:
                log_.exception("Error processing World Bank notice %r, leaving unmarked for retry", t.title[:80])
                continue
    finally:
        if browser is not None:
            browser.close()
        if pw_ctx is not None:
            pw_ctx.stop()

    return match_count
