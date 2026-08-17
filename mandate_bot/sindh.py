"""Scraper for the Sindh Public Procurement Regulatory Authority portal
(ppms.pprasindh.gov.pk) — a JSF/PrimeFaces app (Java, session/ViewState
based, similar in spirit to Punjab's ASP.NET postback pattern but with its
own AJAX protocol). Every document download is a stateful form POST
(`PrimeFaces.addSubmitParam(...).submit(...)`), not a plain GET link, so —
like BPPT — this source is scraped with a real headless browser
(Playwright) rather than plain HTTP requests.

Out of ~2800 total tenders on this portal, the vast majority are already
"Archived"; only ones with Status=Active are worth downloading/scanning.

Each tender's detail modal exposes three kinds of documents:
  - "NIT Notice" — the main announcement (always present)
  - "Bidding Documents" — one per item/scheme (a single NIT can bundle
    several procurement items, each with its own bidding document)
  - "Committee" uploads and "Tender Advertisement" (newspaper scan) docs
Only the first two are downloaded/matched here — committee docs are purely
administrative, and newspaper scans carry the same false-positive risk
already found on EPMS (unrelated articles printed on the same page), so
both are deliberately skipped by targeting specific DOM id patterns.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .logging_utils import append_match_log, slugify
from .matcher import find_matches
from .models import Tender
from .pdf_utils import extract_text

log = logging.getLogger("mandate_bot.sindh")

GOTO_RETRIES = 3
GOTO_RETRY_BACKOFF = [2, 5, 10]


def _goto_with_retry(page, url: str, log_: logging.Logger):
    last_exc = None
    for attempt in range(GOTO_RETRIES):
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            return
        except Exception as exc:
            last_exc = exc
            if attempt < GOTO_RETRIES - 1:
                delay = GOTO_RETRY_BACKOFF[attempt]
                log_.warning("Navigation to %s failed (attempt %d/%d): %s — retrying in %ds",
                             url, attempt + 1, GOTO_RETRIES, exc, delay)
                time.sleep(delay)
    raise last_exc


def _parse_row(row_html: str, base_url: str) -> Tender | None:
    # row_html is a <tr>'s *inner* HTML (bare <td> fragments, no wrapping
    # <tr>) — the HTML parser auto-wraps orphan <td>s in its own
    # table/tbody/tr, so the cells end up nested rather than at the root;
    # recursive=False would find nothing.
    soup = BeautifulSoup(row_html, "lxml")
    tds = soup.find_all("td")
    if len(tds) < 8:
        return None
    return Tender(
        notice_type="Tender",
        title=tds[2].get_text(" ", strip=True),
        category="",  # no procurement-type taxonomy exposed on this portal
        publish_date=tds[4].get_text(" ", strip=True),
        close_date="",
        department=tds[3].get_text(" ", strip=True),
        status=tds[7].get_text(strip=True),
        notice_url=None,   # resolved by re-searching for this NIT ID — see process_candidates
        document_url=None,
        source="sindh",
        tender_ref=tds[0].get_text(strip=True),  # NIT ID, used to re-find this tender later
    )


def fetch_all(base_url: str, listing_path: str, request_delay: float = 1.0, max_pages: int = 20) -> list[Tender]:
    listing_url = urljoin(base_url, listing_path)
    tenders: list[Tender] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(ignore_https_errors=True)
        log.info("Loading %s", listing_url)
        _goto_with_retry(page, listing_url, log)
        page.wait_for_timeout(1500)

        page.click("#PostedNIT\\:statusSerach")
        page.wait_for_timeout(500)
        page.click("#PostedNIT\\:statusSerach_panel li:has-text('Active')")
        page.wait_for_timeout(500)
        page.click("#PostedNIT\\:btnSearch")
        page.wait_for_timeout(2000)

        page_num = 1
        while page_num <= max_pages:
            rows = page.query_selector_all(".ui-datatable-data > tr")
            new_count = 0
            for row in rows:
                t = _parse_row(row.inner_html(), base_url)
                if t and t.tender_ref:
                    tenders.append(t)
                    new_count += 1
            log.info("Sindh (Active) page %d: %d tenders", page_num, new_count)

            next_btn = page.query_selector("a.ui-paginator-next")
            if next_btn is None or "ui-state-disabled" in (next_btn.get_attribute("class") or ""):
                break
            next_btn.click()
            time.sleep(request_delay)
            page.wait_for_timeout(1500)
            page_num += 1

        browser.close()

    return tenders


def _download_all(page, log_: logging.Logger) -> list[str]:
    """Clicks the NIT Notice + every per-item Bidding Document download link
    in the currently-open detail modal, saving each to a temp file. Returns
    the list of saved temp paths."""
    saved_paths = []
    tmpdir = tempfile.mkdtemp()

    link_selectors = [
        "a[id$=':downloadNoticeFileName']",           # NIT Notice
        "a[id*=':itemlist:'][id$=':downloadFileBiddingdoc']",  # per-item bidding docs
    ]
    idx = 0
    for selector in link_selectors:
        links = page.query_selector_all(selector)
        for link in links:
            if not link.is_visible():
                continue
            try:
                with page.expect_download(timeout=20000) as dl_info:
                    link.click()
                download = dl_info.value
                dest = os.path.join(tmpdir, f"doc_{idx}.pdf")
                download.save_as(dest)
                saved_paths.append(dest)
                idx += 1
            except Exception:
                log_.exception("Failed to download a document from Sindh detail modal")
            page.wait_for_timeout(500)

    return saved_paths


def process_candidates(candidates: list[Tender], keywords: list[str], cfg: dict, state, log_: logging.Logger) -> int:
    src_cfg = cfg.get("sindh", {})
    listing_url = urljoin(src_cfg["base_url"], src_cfg["listing_path"])
    request_delay = src_cfg.get("request_delay_seconds", 1.0)

    match_count = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()

        for t in candidates:
            page = browser.new_page(ignore_https_errors=True, accept_downloads=True)
            try:
                _goto_with_retry(page, listing_url, log_)
                page.wait_for_timeout(1000)
                page.fill("#PostedNIT\\:nitCode", t.tender_ref)
                page.click("#PostedNIT\\:btnSearch")
                page.wait_for_timeout(1500)

                view_link = page.query_selector("a[aria-label='View NIT Details']")
                if view_link is None:
                    log_.warning("Could not find %s on re-search, skipping", t.tender_ref)
                    page.close()
                    continue
                view_link.click()
                page.wait_for_timeout(1500)

                saved_paths = _download_all(page, log_)
            except Exception:
                log_.exception("Error opening detail modal for %s", t.tender_ref)
                page.close()
                continue

            page.close()
            time.sleep(request_delay)

            if not saved_paths:
                state.mark(t.key)
                continue

            combined_text = ""
            for path in saved_paths:
                combined_text += "\n" + extract_text(path)

            hits = find_matches(combined_text, keywords)
            if hits:
                match_count += 1
                # publish_date often looks like "0 = 12-02-2025" (the "0"
                # being a corrigendum counter from the same table cell)
                date_part = re.sub(r"^\d+\s*=\s*", "", t.publish_date).strip()
                folder_name = f"{slugify(date_part)}_{slugify(t.title)}"
                dest_dir = os.path.join(cfg["paths"]["download_dir"], folder_name)
                os.makedirs(dest_dir, exist_ok=True)
                for i, path in enumerate(saved_paths):
                    with open(path, "rb") as src_f, open(os.path.join(dest_dir, f"doc_{i}.pdf"), "wb") as dst_f:
                        dst_f.write(src_f.read())

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
                    "notice_url": "",
                    "document_url": "",
                    "saved_dir": dest_dir,
                    "tender_ref": t.tender_ref,
                    "extra_urls": "",
                })

            state.mark(t.key)

        browser.close()

    return match_count
