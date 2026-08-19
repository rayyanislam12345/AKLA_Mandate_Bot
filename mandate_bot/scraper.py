"""Scraper for the Punjab (Pakistan) e-Procurement portal (eproc.punjab.gov.pk).
An ASP.NET WebForms `RadGrid`, paged via the standard `__doPostBack(target,
argument)` client-side JS function.

Originally scraped with plain `requests` (2026-08-15), but the portal
started blocking non-browser HTTP clients at the connection level
(confirmed 2026-08-19: identical requests fail via curl and Python
`requests` — and even Playwright's lightweight APIRequestContext — with
`SSL: WRONG_VERSION_NUMBER`, while a full Playwright page navigation
succeeds every time). So both the listing and every document download now
go through a real browser via Playwright, calling the page's own
`__doPostBack` function directly for pagination rather than manually
replaying form/viewstate payloads.
"""
from __future__ import annotations

import logging
import os
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

log = logging.getLogger("mandate_bot.scraper")

GRID_TABLE_CLASS = "rgMasterTable"
# Column order confirmed from the live page header row (2026-08-15):
# Procurement Title | Procurement Name | Type | Publish Date | Close Date |
# Department | Status | Tender Notice | Bidding Document
COL_NOTICE_TYPE, COL_TITLE, COL_CATEGORY, COL_PUBLISH, COL_CLOSE, COL_DEPT, COL_STATUS, COL_NOTICE_LINK, COL_DOC_LINK = range(9)

GOTO_RETRIES = 3
GOTO_RETRY_BACKOFF = [2, 5, 10]


def _parse_rows(soup: BeautifulSoup, listing_url: str) -> list[Tender]:
    table = soup.find("table", class_=GRID_TABLE_CLASS)
    if table is None:
        return []
    tbody = table.find("tbody")
    if tbody is None:
        return []
    tenders = []
    for row in tbody.find_all("tr", recursive=False):
        classes = row.get("class") or []
        if not any(c in ("rgRow", "rgAltRow") for c in classes):
            continue
        tds = row.find_all("td", recursive=False)
        if len(tds) < 9:
            continue

        def cell_text(i):
            return tds[i].get_text(strip=True)

        def cell_link(i):
            a = tds[i].find("a")
            if a and a.get("href"):
                return urljoin(listing_url, a["href"])
            return None

        tenders.append(Tender(
            notice_type=cell_text(COL_NOTICE_TYPE),
            title=cell_text(COL_TITLE),
            category=cell_text(COL_CATEGORY),
            publish_date=cell_text(COL_PUBLISH),
            close_date=cell_text(COL_CLOSE),
            department=cell_text(COL_DEPT),
            status=cell_text(COL_STATUS),
            notice_url=cell_link(COL_NOTICE_LINK),
            document_url=cell_link(COL_DOC_LINK),
        ))
    return tenders


def _goto_and_capture(page, url: str, log_: logging.Logger) -> str:
    """Navigates and returns the RAW response body text rather than
    page.content(). Punjab's RadGrid data rows are present in the server's
    response — confirmed via the raw network body — but get wiped from the
    live DOM by client-side JS shortly after load specifically when driven
    by Playwright (page.content() reflects only a lone pager row, no data,
    even after a long wait); parsing the raw response sidesteps that
    entirely, and is what the pagination capture below does too."""
    last_exc = None
    for attempt in range(GOTO_RETRIES):
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return resp.text()
        except Exception as exc:
            last_exc = exc
            if attempt < GOTO_RETRIES - 1:
                delay = GOTO_RETRY_BACKOFF[attempt]
                log_.warning("Navigation to %s failed (attempt %d/%d): %s — retrying in %ds",
                             url, attempt + 1, GOTO_RETRIES, exc, delay)
                time.sleep(delay)
    raise last_exc


def fetch_all(base_url: str, listing_path: str, verify_ssl: bool = True,
              request_delay: float = 1.5, max_pages: int = 20) -> list[Tender]:
    listing_url = urljoin(base_url, listing_path)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(ignore_https_errors=not verify_ssl)

        log.info("Fetching page 1: %s", listing_url)
        html = _goto_and_capture(page, listing_url, log)
        soup = BeautifulSoup(html, "lxml")
        all_tenders = _parse_rows(soup, listing_url)
        log.info("Page 1: %d rows", len(all_tenders))

        page_num = 1
        while page_num < max_pages:
            # the pager itself (unlike the data rows) does stay live in the
            # DOM, so it's still queried the normal way
            target_label = str(page_num + 1)
            pager = page.query_selector("div.rgNumPart")
            if pager is None:
                break
            link = next((a for a in pager.query_selector_all("a")
                         if a.inner_text().strip() == target_label), None)
            if link is None:
                break

            time.sleep(request_delay)
            log.info("Fetching page %d", page_num + 1)
            try:
                with page.expect_response(lambda r: listing_url in r.url, timeout=30000) as resp_info:
                    link.click()
                html = resp_info.value.text()
            except Exception:
                log.exception("Loading page %d failed, stopping here", page_num + 1)
                break

            soup = BeautifulSoup(html, "lxml")
            rows = _parse_rows(soup, listing_url)
            if not rows:
                break
            all_tenders.extend(rows)
            log.info("Page %d: %d rows (total %d)", page_num + 1, len(rows), len(all_tenders))
            page_num += 1

        browser.close()

    return all_tenders


def _download_via_browser(page, url: str, dest_path: str, log_: logging.Logger) -> bool:
    """Direct-linked PDFs make Playwright treat page.goto() as a download
    and raise "Download is starting" — that's expected, not a failure; the
    download itself still completes and is captured by expect_download."""
    last_exc = None
    for attempt in range(GOTO_RETRIES):
        try:
            with page.expect_download(timeout=20000) as dl_info:
                try:
                    page.goto(url, timeout=20000)
                except Exception as goto_err:
                    if "Download is starting" not in str(goto_err):
                        raise
            dl_info.value.save_as(dest_path)
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < GOTO_RETRIES - 1:
                delay = GOTO_RETRY_BACKOFF[attempt]
                log_.warning("Download of %s failed (attempt %d/%d): %s — retrying in %ds",
                             url, attempt + 1, GOTO_RETRIES, exc, delay)
                time.sleep(delay)
    log_.warning("Failed to download %s after %d attempts: %s", url, GOTO_RETRIES, last_exc)
    return False


def process_candidates(candidates: list[Tender], keywords: list[str], cfg: dict, state, log_: logging.Logger) -> int:
    src_cfg = cfg["source"]
    verify_ssl = not src_cfg.get("insecure_skip_verify", False)
    request_delay = src_cfg.get("request_delay_seconds", 1.5)

    match_count = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(ignore_https_errors=not verify_ssl, accept_downloads=True)

        for t in candidates:
            urls = [u for u in (t.document_url, t.notice_url) if u]
            if not urls:
                state.mark(t.key)
                continue

            combined_text = ""
            tmp_files = []
            any_failed = False
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    for i, url in enumerate(urls):
                        tmp_path = os.path.join(tmpdir, f"doc_{i}.pdf")
                        ok = _download_via_browser(page, url, tmp_path, log_)
                        if ok:
                            tmp_files.append((url, tmp_path))
                            combined_text += "\n" + extract_text(tmp_path)
                        else:
                            any_failed = True
                        time.sleep(request_delay)

                    hits = find_matches(combined_text, keywords)
                    if hits:
                        match_count += 1
                        folder_name = f"{t.publish_date.replace(' ', '')}_{slugify(t.title)}".replace("/", "-")
                        dest_dir = os.path.join(cfg["paths"]["download_dir"], folder_name)
                        os.makedirs(dest_dir, exist_ok=True)
                        for url, tmp_path in tmp_files:
                            dest_name = os.path.basename(url)
                            with open(tmp_path, "rb") as src_f, open(os.path.join(dest_dir, dest_name), "wb") as dst_f:
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
                            "notice_url": t.notice_url or "",
                            "document_url": t.document_url or "",
                            "saved_dir": dest_dir,
                            "tender_ref": t.tender_ref,
                            "extra_urls": "; ".join(t.extra_urls),
                        })
            except Exception:
                log_.exception("Error processing tender %s", t.title)
                continue

            if any_failed:
                log_.warning("Leaving %s unmarked (seen) for retry — one or more documents failed to download", t.title)
            else:
                state.mark(t.key)

        browser.close()

    return match_count
