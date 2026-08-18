"""Scraper for the KP Planning & Development Department tenders page
(pndkp.gov.pk/tenders/) — a WordPress site using the "WP Download Manager"
plugin. The simplest source yet: everything (title + a directly downloadable
file URL) is right there in the listing page itself, no detail-page visit
needed, no pagination (the whole tenders category fits on one page), and a
valid TLS certificate. Plain `requests`, no browser needed.

No department/category/closing-date metadata is exposed — this plugin is a
generic file library, not a purpose-built tender portal — so every listed
file is scanned directly via the keyword pass rather than being pre-filtered.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .http_utils import get_with_retry
from .logging_utils import append_match_log, unique_dest_dir
from .matcher import find_matches
from .models import Tender
from .pdf_utils import download_file, extract_text_any

log = logging.getLogger("mandate_bot.pndkp")

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MandateBot/1.0"}


def _parse_listing(html: str, base_url: str) -> list[Tender]:
    soup = BeautifulSoup(html, "lxml")
    tenders = []
    for card in soup.select("div.link-template-default.card"):
        title_a = card.find("h3", class_="package-title")
        title_a = title_a.find("a") if title_a else None
        dl_a = card.find("a", class_="wpdm-download-link")
        if not title_a or not dl_a or not dl_a.get("data-downloadurl"):
            continue
        tenders.append(Tender(
            notice_type="Tender",
            title=title_a.get_text(strip=True),
            category="",
            publish_date="",
            close_date="",
            department="",
            status="",
            notice_url=urljoin(base_url, title_a["href"]),  # detail page, for reference only
            document_url=dl_a["data-downloadurl"],
            source="pndkp",
        ))
    return tenders


def fetch_all(base_url: str, listing_path: str, request_delay: float = 1.0, max_pages: int = 5) -> list[Tender]:
    session = requests.Session()
    session.headers.update(HEADERS)
    listing_url = urljoin(base_url, listing_path)

    log.info("Loading %s", listing_url)
    resp = get_with_retry(session, listing_url, log, timeout=30)
    tenders = _parse_listing(resp.text, base_url)
    log.info("PNDKP: %d tenders found", len(tenders))
    return tenders


def _guess_extension(path: str) -> str:
    """This portal serves a mix of PDFs, scanned images, and Word docs, so
    the saved filename should reflect what it actually is rather than
    always claiming .pdf."""
    with open(path, "rb") as f:
        header = f.read(8)
    if header[:4] == b"%PDF":
        return ".pdf"
    if header[:4] == b"PK\x03\x04":
        return ".docx"
    if header[:2] == b"\xff\xd8":
        return ".jpg"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ".bin"


def process_candidates(candidates: list[Tender], keywords: list[str], cfg: dict, state, log_: logging.Logger) -> int:
    src_cfg = cfg.get("pndkp", {})
    request_delay = src_cfg.get("request_delay_seconds", 1.0)

    session = requests.Session()
    session.headers.update(HEADERS)

    match_count = 0
    for t in candidates:
        if not t.document_url:
            state.mark(t.key)
            continue

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = os.path.join(tmpdir, "doc_0.bin")
            ok = download_file(session, t.document_url, tmp_path, verify_ssl=True)
            time.sleep(request_delay)

            if not ok:
                log_.warning("Leaving %s unmarked (seen) for retry — document failed to download", t.title)
                continue

            text = extract_text_any(tmp_path)
            hits = find_matches(text, keywords)
            if hits:
                match_count += 1
                dest_dir = unique_dest_dir(cfg["paths"]["download_dir"], t.title)
                os.makedirs(dest_dir, exist_ok=True)
                dest_name = f"doc_0{_guess_extension(tmp_path)}"
                with open(tmp_path, "rb") as src_f, open(os.path.join(dest_dir, dest_name), "wb") as dst_f:
                    dst_f.write(src_f.read())

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

    return match_count
