"""Scraper for the Asian Development Bank's Consultant Management System
(selfservice.adb.org) — an Oracle E-Business Suite / OA Framework app.
International-tier consulting opportunities (loans/grants/TAs across ADB's
member countries), a different tier from the domestic Pakistani portals.

Despite living behind a login-capable "self service" portal, browsing,
searching, viewing full opportunity details, and downloading Terms of
Reference attachments are all public — no login required. (Only "Express
Interest", i.e. actually submitting a bid, needs an account, which this bot
never does.) So although credentials were provided for this site, they are
NOT used here — see secrets.yaml if that ever changes.

The "Search by Expertise" box only searches the Expertise tag field, not
project titles or reference numbers, so there's no way to re-find a specific
opportunity by ID later the way other sources do. Given the result set for
legal-relevant searches is small (a few dozen), this source runs fetch and
document-download as a single pass per search term instead of the usual
two-phase fetch_all()/process_candidates() split — already-seen rows are
skipped by title before ever clicking into them, so re-runs stay cheap.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

from .logging_utils import append_match_log, unique_dest_dir
from .models import Tender

log = logging.getLogger("mandate_bot.adb")

HOME_URL = "https://selfservice.adb.org/OA_HTML/OA.jsp?OAFunc=XXCRS_CSRN_HOME_PAGE"

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


def _search(page, term: str, log_: logging.Logger):
    _goto_with_retry(page, HOME_URL, log_)
    page.wait_for_timeout(1500)
    page.fill("input[type=text] >> nth=0", term)
    page.click("text=Go")
    page.wait_for_timeout(2000)


def _parse_rows(page) -> list[dict]:
    """Reads the results table without navigating away — title text alone
    is enough to build a dedupe key, so new/seen can be decided before
    ever clicking into a row."""
    rows = []
    for link in page.query_selector_all("table a"):
        title = link.inner_text().strip()
        # real project rows have long, distinctive titles; nav/filter chrome doesn't
        if len(title) > 20 and "(" in title:
            rows.append({"title": title})
    return rows


def _make_tender(row: dict) -> Tender:
    return Tender(
        notice_type="Consulting Opportunity",
        title=row["title"],
        category="",
        publish_date="",
        close_date="",
        department="Asian Development Bank",
        status="",
        notice_url=None,
        document_url=None,
        source="adb",
    )


def run(search_terms: list[str], cfg: dict, state, log_: logging.Logger) -> tuple[int, int]:
    """Combined fetch+process (see module docstring for why). No keywords
    parameter — being returned by the ADB Expertise search is itself the
    match signal here, unlike every other source. Returns
    (checked_count, match_count)."""
    checked = 0
    match_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(ignore_https_errors=True, accept_downloads=True)

        seen_titles_this_run = set()
        for term in search_terms:
            try:
                _search(page, term, log_)
            except Exception:
                log_.exception("ADB search for %r failed, skipping this term", term)
                continue

            rows = _parse_rows(page)
            log_.info("ADB search %r: %d rows", term, len(rows))

            for row in rows:
                t = _make_tender(row)
                if t.key in seen_titles_this_run:
                    continue  # same opportunity can match multiple search terms
                seen_titles_this_run.add(t.key)

                if state.has(t.key):
                    continue
                checked += 1

                # ADB's "Expertise" field is a tag curated by ADB staff, not
                # generic boilerplate text — being returned by the search
                # itself is the signal, so every new candidate here counts
                # as a match. The TOR is downloaded for reference/reading,
                # not as a further filter.
                try:
                    # re-run the search fresh so the results table (and its
                    # View-CSRN icon indices) are in a known, valid state
                    _search(page, term, log_)
                    fresh_rows = _parse_rows(page)
                    row_index = next((i for i, r in enumerate(fresh_rows) if r["title"] == row["title"]), None)
                    if row_index is None:
                        log_.warning("Could not re-locate %r after re-search, skipping", row["title"][:80])
                        continue

                    imgs = page.query_selector_all("img[src*='view'], img[alt*='View']")
                    if row_index >= len(imgs):
                        log_.warning("Row index out of range for %r, skipping", row["title"][:80])
                        continue
                    imgs[row_index].click()
                    page.wait_for_timeout(2000)

                    dest_dir = unique_dest_dir(cfg["paths"]["download_dir"], t.title)
                    doc_idx = 0
                    saved_any = False

                    # Any real attachment file (e.g. a "TOR" link in the
                    # Profile tab's attachments table) — present on some
                    # postings (typically Firm/QCBS ones), not others.
                    tor_links = [l for l in page.query_selector_all("a") if l.inner_text().strip() == "TOR"]
                    if tor_links:
                        with tempfile.TemporaryDirectory() as tmpdir:
                            tmp_path = os.path.join(tmpdir, "attachment.pdf")
                            try:
                                with page.expect_download(timeout=20000) as dl_info:
                                    tor_links[0].click()
                                dl_info.value.save_as(tmp_path)
                                os.makedirs(dest_dir, exist_ok=True)
                                with open(tmp_path, "rb") as src_f, \
                                     open(os.path.join(dest_dir, f"doc_{doc_idx}_attachment.pdf"), "wb") as dst_f:
                                    dst_f.write(src_f.read())
                                doc_idx += 1
                                saved_any = True
                            except Exception:
                                log_.warning("Failed to download attachment for %r", row["title"][:80])

                    # The "Terms of Reference" tab always exists and always
                    # has the full content rendered inline (whether or not a
                    # separate attachment also exists) — snapshot it as a
                    # PDF, same pattern as BPPT's rendered-page documents.
                    tor_tab = page.query_selector("text=Terms of Reference")
                    if tor_tab:
                        try:
                            tor_tab.click()
                            page.wait_for_timeout(1500)
                            os.makedirs(dest_dir, exist_ok=True)
                            page.pdf(path=os.path.join(dest_dir, f"doc_{doc_idx}_terms_of_reference.pdf"))
                            saved_any = True
                        except Exception:
                            log_.warning("Failed to snapshot Terms of Reference tab for %r", row["title"][:80])

                    if not saved_any:
                        log_.warning("Leaving %r unmarked (seen) for retry — no document captured",
                                     row["title"][:80])
                        continue

                    match_count += 1
                    log_.info("MATCH: %s (via search term %r)", t.title, term)
                    append_match_log(cfg["paths"]["match_log"], {
                        "found_at": datetime.now().isoformat(timespec="seconds"),
                        "source": t.source,
                        "title": t.title,
                        "department": t.department,
                        "category": t.category,
                        "notice_type": t.notice_type,
                        "publish_date": t.publish_date,
                        "close_date": t.close_date,
                        "matched_keywords": f"[ADB Expertise search: {term!r}]",
                        "notice_url": "",
                        "document_url": "",
                        "saved_dir": dest_dir,
                        "tender_ref": t.tender_ref,
                        "extra_urls": "",
                    })

                    state.mark(t.key)
                except Exception:
                    log_.exception("Error processing ADB opportunity %r", row["title"][:80])
                    continue

        browser.close()

    return checked, match_count
