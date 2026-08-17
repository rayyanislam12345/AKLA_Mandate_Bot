"""Scraper for the Balochistan procurement portal (bpptwo.vdc.services), a
Blazor Server app. Pagination and category filtering run over a live
WebSocket connection to the server, so plain HTTP requests can only ever see
page 1 — a real browser (Playwright) is required to click through pages.

The "Bidding Document" / "NIT Report" links are also not real PDFs: they're
Angular-templated report pages that render their content client-side, so a
plain HTTP fetch returns unrendered `{{...}}` placeholders. Those are
rendered with Playwright too, and a PDF snapshot is printed via
`page.pdf()` for anything that matches, so a real file still lands in the
downloads folder.
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

log = logging.getLogger("mandate_bot.bppt")

GOTO_RETRIES = 3
GOTO_RETRY_BACKOFF = [2, 5, 10]  # seconds, one per retry attempt


def _goto_with_retry(page, url: str, log_: logging.Logger):
    """Navigate with retries — WiFi drops/roaming show up as
    net::ERR_NETWORK_CHANGED (and similar transient net:: errors) and are
    worth a few attempts before giving up on a document."""
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


def _parse_row(row, category: str, base_url: str) -> Tender | None:
    tds = row.find_all("td", recursive=False)
    if len(tds) < 6:
        return None

    title_cell = tds[1]
    title_link = title_cell.find("a", class_="cursor-pointer")
    lines = [l.strip() for l in title_link.get_text(separator="\n").split("\n") if l.strip()] if title_link else []
    # lines[0] is the TSE reference number, lines[1] is the actual title
    title = lines[1] if len(lines) > 1 else (lines[0] if lines else title_cell.get_text(" ", strip=True))

    links = {a.get("title"): urljoin(base_url, a["href"]) for a in title_cell.find_all("a", href=True) if a.get("href")}

    date_text = tds[2].get_text(" ", strip=True)
    publish_m = re.search(r"Publish:\s*([\d/]+)", date_text)
    close_m = re.search(r"Dead\s*Line:\s*([\d/]+)", date_text)

    return Tender(
        notice_type="Tender",
        title=title,
        category=category,
        publish_date=publish_m.group(1) if publish_m else "",
        close_date=close_m.group(1) if close_m else "",
        department=tds[3].get_text(" ", strip=True),
        status="",
        notice_url=links.get("NIT Report"),
        document_url=links.get("Bidding Document"),
        source="bppt",
    )


def fetch_all(base_url: str, listing_path: str, categories: list[str],
              request_delay: float = 1.0, max_pages: int = 30) -> list[Tender]:
    tenders: list[Tender] = []
    seen_keys: set[str] = set()
    listing_url = urljoin(base_url, listing_path)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        log.info("Loading %s", listing_url)
        _goto_with_retry(page, listing_url, log)
        page.wait_for_timeout(1500)

        for category in categories:
            btn = page.query_selector(f"button:has-text('{category}')")
            if not btn:
                log.warning("Category button %r not found, skipping", category)
                continue
            btn.click()
            page.wait_for_timeout(2000)

            page_num = 1
            while page_num <= max_pages:
                soup = BeautifulSoup(page.content(), "lxml")
                table = soup.find("table", class_="table")
                tbody = table.find("tbody") if table else None
                rows = tbody.find_all("tr", recursive=False) if tbody else []
                if not rows:
                    break

                new_in_page = 0
                for row in rows:
                    t = _parse_row(row, category, base_url)
                    if t and t.key not in seen_keys:
                        seen_keys.add(t.key)
                        tenders.append(t)
                        new_in_page += 1
                log.info("%s page %d: %d rows (%d new)", category, page_num, len(rows), new_in_page)

                if new_in_page == 0 and page_num > 1:
                    # Pagination click had no effect (same rows re-shown) —
                    # seen on categories with very few/no real entries.
                    log.info("%s: page %d repeated the previous page, stopping", category, page_num)
                    break

                next_li = None
                for li in page.query_selector_all(".pagination li"):
                    if li.query_selector("i.fa-angle-right"):
                        next_li = li
                        break
                if next_li is None or "disabled" in (next_li.get_attribute("class") or ""):
                    break

                next_li.click()
                time.sleep(request_delay)
                page.wait_for_timeout(1500)
                page_num += 1

        browser.close()

    return tenders


def process_candidates(candidates: list[Tender], keywords: list[str], cfg: dict, state, log_: logging.Logger) -> int:
    """Render each candidate's documents, keyword-match the text, save PDF
    snapshots of matches, and mark every candidate as seen. Returns the
    number of matches found."""
    match_count = 0
    src_cfg = cfg.get("bppt", {})
    request_delay = src_cfg.get("request_delay_seconds", 1.0)

    with sync_playwright() as p:
        browser = p.chromium.launch()

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
                    page = browser.new_page()
                    for i, url in enumerate(urls):
                        try:
                            _goto_with_retry(page, url, log_)
                            page.wait_for_timeout(1000)
                            combined_text += "\n" + page.inner_text("body")
                            pdf_path = os.path.join(tmpdir, f"doc_{i}.pdf")
                            page.pdf(path=pdf_path)
                            tmp_files.append((url, pdf_path))
                        except Exception:
                            log_.exception("Failed to render %s", url)
                            any_failed = True
                        time.sleep(request_delay)
                    page.close()

                    hits = find_matches(combined_text, keywords)
                    if hits:
                        match_count += 1
                        folder_name = f"{t.publish_date.replace('/', '-')}_{slugify(t.title)}"
                        dest_dir = os.path.join(cfg["paths"]["download_dir"], folder_name)
                        os.makedirs(dest_dir, exist_ok=True)
                        for i, (url, tmp_path) in enumerate(tmp_files):
                            dest_name = f"doc_{i}.pdf"
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
                        })
            except Exception:
                log_.exception("Error processing BPPT tender %s", t.title)
                continue

            if any_failed:
                log_.warning("Leaving %s unmarked (seen) for retry — one or more documents failed to render", t.title)
            else:
                state.mark(t.key)

        browser.close()

    return match_count
