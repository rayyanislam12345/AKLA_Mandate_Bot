"""Scraper for the federal PPRA e-Publish & Monitoring System
(epms.ppra.gov.pk) — Pakistan's national procurement portal, as opposed to
the provincial ones (Punjab, Balochistan/BPPT, KPPRA). Classic
server-rendered pages with query-string pagination AND a working
server-side category filter (procurement_category=3/4), so this source is
scraped with plain `requests` — no browser automation needed, and no need
to classify every single tender individually like KPPRA (the filter does
that work server-side).

The listing table doesn't link documents directly — each tender's detail
page (public/tenders/tender-details/{id}) has to be visited to get the
actual "Download Tender Document" / "Download Advertisement" PDF links.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .logging_utils import append_match_log, slugify
from .matcher import find_matches
from .models import Tender
from .pdf_utils import download_pdf, extract_text

log = logging.getLogger("mandate_bot.epms")

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MandateBot/1.0"}

# procurement_category select option values, from the live filter form
CATEGORY_IDS = {
    "goods": "1",
    "works": "2",
    "consultancy services": "3",
    "non-consultancy services": "4",
}


def _parse_listing_rows(html: str, category: str) -> list[Tender]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    tbody = table.find("tbody") if table else None
    if tbody is None:
        return []

    tenders = []
    for row in tbody.find_all("tr", recursive=False):
        tds = row.find_all("td", recursive=False)
        if len(tds) != 8:
            continue
        detail_a = tds[7].find("a", href=re.compile(r"/tender-details/"))
        if not detail_a:
            continue
        tenders.append(Tender(
            notice_type="Tender",
            title=tds[2].get_text(" ", strip=True),
            category=category,
            publish_date=tds[5].get_text(" ", strip=True),
            close_date=tds[6].get_text(" ", strip=True),
            department=tds[3].get_text(" ", strip=True),
            status=tds[4].get_text(strip=True),
            notice_url=detail_a["href"],  # detail page, resolved to docs in process_candidates
            document_url=None,
            source="epms",
            tender_ref=tds[1].get_text(strip=True),
        ))
    return tenders


def fetch_all(base_url: str, listing_path: str, categories: list[str],
              verify_ssl: bool = True, request_delay: float = 0.5, max_pages: int = 20) -> list[Tender]:
    session = requests.Session()
    session.headers.update(HEADERS)
    listing_url = urljoin(base_url, listing_path)

    tenders: list[Tender] = []
    for category in categories:
        cat_id = CATEGORY_IDS.get(category.lower())
        if cat_id is None:
            log.warning("Unknown EPMS category %r, skipping", category)
            continue

        page_num = 1
        while page_num <= max_pages:
            resp = session.get(listing_url, params={"procurement_category": cat_id, "page": page_num},
                                verify=verify_ssl, timeout=30)
            resp.raise_for_status()
            rows = _parse_listing_rows(resp.text, category)
            if not rows:
                break
            log.info("EPMS %s page %d: %d rows", category, page_num, len(rows))
            tenders.extend(rows)
            page_num += 1
            time.sleep(request_delay)

    # detail-page URLs are absolute already resolved below; dedupe by that URL
    seen = set()
    deduped = []
    for t in tenders:
        key = urljoin(base_url, t.notice_url)
        if key not in seen:
            seen.add(key)
            t.notice_url = key
            deduped.append(t)
    return deduped


def _extract_doc_links(session: requests.Session, base_url: str, detail_url: str, verify_ssl: bool) -> list[str]:
    resp = session.get(detail_url, verify=verify_ssl, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    links = []
    for a in soup.find_all("a", href=re.compile(r"/pdf\?file=")):
        links.append(urljoin(base_url, a["href"]))
    return links


def process_candidates(candidates: list[Tender], keywords: list[str], cfg: dict, state, log_: logging.Logger) -> int:
    src_cfg = cfg.get("epms", {})
    verify_ssl = not src_cfg.get("insecure_skip_verify", False)
    request_delay = src_cfg.get("request_delay_seconds", 0.5)
    base_url = src_cfg["base_url"]

    session = requests.Session()
    session.headers.update(HEADERS)

    match_count = 0
    for t in candidates:
        try:
            doc_urls = _extract_doc_links(session, base_url, t.notice_url, verify_ssl)
        except Exception:
            log_.warning("Failed to load detail page for %s", t.title)
            continue  # leave unmarked, retry next run
        time.sleep(request_delay)

        if not doc_urls:
            state.mark(t.key)
            continue

        combined_text = ""
        tmp_files = []
        any_failed = False
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                for i, url in enumerate(doc_urls):
                    tmp_path = os.path.join(tmpdir, f"doc_{i}.pdf")
                    ok = download_pdf(session, url, tmp_path, verify_ssl=verify_ssl)
                    if ok:
                        tmp_files.append((url, tmp_path))
                        combined_text += "\n" + extract_text(tmp_path)
                    else:
                        any_failed = True
                    time.sleep(request_delay)

                hits = find_matches(combined_text, keywords)
                if hits:
                    match_count += 1
                    folder_name = f"{t.publish_date.replace(' ', '')}_{slugify(t.title)}".replace("/", "-").replace(",", "")
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
                        "document_url": "; ".join(doc_urls),
                        "saved_dir": dest_dir,
                        "tender_ref": t.tender_ref,
                        "extra_urls": "",  # doc_urls above already covers everything found for this source
                    })
        except Exception:
            log_.exception("Error processing EPMS tender %s", t.title)
            continue

        if any_failed:
            log_.warning("Leaving %s unmarked (seen) for retry — one or more documents failed to download", t.title)
        else:
            state.mark(t.key)

    return match_count
