"""Scraper for WBGeProcure / RFx Now (wbgeprocure-rfxnow.worldbank.org) —
the World Bank Group's own corporate procurement platform, distinct from
`mandate_bot/worldbank.py` (which covers procurement financed by the Bank
on behalf of borrower countries). This one is WBG buying goods/services
for itself, open globally to any vendor — there's no country field to
filter on, unlike the other World Bank source.

An Angular SPA, but the whole active-advertisements listing is served by
one clean, unauthenticated JSON endpoint
(`json/advertisement/activeAdvertisements.json`) — no pagination needed,
it returns the full active list (25 rows as of 2026-08-20) in one call.
Each record already includes its full description as an HTML fragment
(`text`), same convenient shape as the other World Bank source, so
listing and keyword-matching both happen off this one call.

About half of postings also have a real downloadable attachment (usually
a "Terms of Reference" PDF or .docx) — checked via a per-advertisement
`json/advertisement/{id}/advertisementAttachmentsForPublic.json` endpoint
and downloaded from `waffle/upload/advertisementAttachment/{documentId}/
file.html`, both plain unauthenticated GETs, confirmed via curl (no
Playwright needed for any of this). For the postings that don't have one,
a headless browser renders the `text` HTML into a PDF snapshot instead —
same fallback pattern as worldbank.py — used only for confirmed matches.

TLS: `requests`' default certifi bundle verifies this host fine even
though Python's stdlib `urllib` doesn't (an environment quirk, not a real
cert problem) — confirmed by testing both directly, so no
insecure_skip_verify flag is needed here.
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
from .pdf_utils import download_file

log = logging.getLogger("mandate_bot.wbgeprocure")

BASE_URL = "https://wbgeprocure-rfxnow.worldbank.org/rfxnow"
LISTING_URL = f"{BASE_URL}/json/advertisement/activeAdvertisements.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MandateBot/1.0"}

TAG_RE = re.compile(r"<[^>]+>")
AD_ID_RE = re.compile(r"/advertisement/(\d+)/view")


def _html_to_text(raw_html: str) -> str:
    return html.unescape(TAG_RE.sub(" ", raw_html or ""))


def _extract_ad_id(notice_url: str) -> str | None:
    m = AD_ID_RE.search(notice_url or "")
    return m.group(1) if m else None


def _parse_record(rec: dict) -> Tender:
    ad_id = rec.get("id")
    return Tender(
        notice_type="Advertisement",
        title=rec.get("procurementTitle") or "(untitled)",
        category="",
        publish_date=(rec.get("publicationDate") or "")[:10],
        close_date=(rec.get("eoiDeadline") or "")[:10],
        department="World Bank Group",
        status="",
        notice_url=f"{BASE_URL}/public/advertisement/{ad_id}/view.html" if ad_id else None,
        document_url=None,
        source="wbgeprocure",
        tender_ref=rec.get("procurementNumber") or str(ad_id or ""),
        raw_content=rec.get("text", ""),
    )


def fetch_all() -> list[Tender]:
    session = requests.Session()
    session.headers.update(HEADERS)

    log.info("Fetching %s", LISTING_URL)
    resp = get_with_retry(session, LISTING_URL, log, timeout=30)
    records = resp.json().get("advertisementList", [])
    tenders = [_parse_record(r) for r in records if not r.get("draft")]
    log.info("WBGeProcure: %d active advertisements fetched", len(tenders))
    return tenders


def process_candidates(candidates: list[Tender], keywords: list[str], cfg: dict, state, log_: logging.Logger) -> int:
    src_cfg = cfg.get("wbgeprocure", {})
    request_delay = src_cfg.get("request_delay_seconds", 0.5)

    session = requests.Session()
    session.headers.update(HEADERS)

    match_count = 0
    pw_ctx = None
    browser = None

    try:
        for t in candidates:
            # Title scanned alongside the body — see worldbank.py's
            # find_matches call for why (same API-sourced-text situation:
            # notice_text here paraphrases rather than repeats the title
            # verbatim, e.g. "Legal Consultant" in the title vs. "Legal
            # and Regulatory Consultant" in the body).
            hits = find_matches(t.title + "\n" + _html_to_text(t.raw_content), keywords)
            if not hits:
                state.mark(t.key)
                continue

            try:
                ad_id = _extract_ad_id(t.notice_url)
                dest_dir = unique_dest_dir(cfg["paths"]["download_dir"], t.source, t.tender_ref or t.title)
                os.makedirs(dest_dir, exist_ok=True)
                saved_any = False

                if ad_id:
                    attach_url = f"{BASE_URL}/json/advertisement/{ad_id}/advertisementAttachmentsForPublic.json"
                    resp = get_with_retry(session, attach_url, log_, timeout=20)
                    attachments = resp.json().get("advertisementAttachmentList", [])
                    for i, a in enumerate(attachments):
                        doc = a.get("document") or {}
                        doc_id = doc.get("id")
                        if not doc_id:
                            continue
                        ext = doc.get("extension") or ".bin"
                        dl_url = f"{BASE_URL}/waffle/upload/advertisementAttachment/{doc_id}/file.html"
                        dest_path = os.path.join(dest_dir, f"doc_{i}{ext}")
                        if download_file(session, dl_url, dest_path, verify_ssl=True):
                            saved_any = True
                        time.sleep(request_delay)

                if not saved_any:
                    try:
                        if browser is None:
                            pw_ctx = sync_playwright().start()
                            browser = pw_ctx.chromium.launch()
                        page = browser.new_page()
                        page.set_content(f"<html><body>{t.raw_content}</body></html>")
                        page.pdf(path=os.path.join(dest_dir, "notice.pdf"))
                        page.close()
                        saved_any = True
                    except Exception:
                        log_.warning("Failed to render PDF snapshot for %r, saving raw HTML instead", t.title[:80])
                        with open(os.path.join(dest_dir, "notice.html"), "w", encoding="utf-8") as f:
                            f.write(t.raw_content)
                        saved_any = True

                match_count += 1
                log_.info("MATCH: %s — keywords: %s", t.title, hits)
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
                log_.exception("Error processing WBGeProcure advertisement %r, leaving unmarked for retry", t.title[:80])
                continue
    finally:
        if browser is not None:
            browser.close()
        if pw_ctx is not None:
            pw_ctx.stop()

    return match_count
