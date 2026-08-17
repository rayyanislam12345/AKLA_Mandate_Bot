"""Scraper for the Khyber Pakhtunkhwa Public Procurement Regulatory
Authority portal (kppra.gov.pk). Classic server-rendered PHP with
query-string pagination (?p=2) — no browser automation needed here.

The listing table doesn't show a tender's procurement category directly;
that only comes back from a small AJAX JSON endpoint
(includes/class.tender.php?getTenderDetails=yes&tender_id=N), which is
called once per listed tender to classify it. Documents are a mix of PDFs
and photographed/scanned JPGs (some tender_file entries are camera photos
of a physical notice board), so both file types are handled: PDFs go
through the existing pdfplumber+OCR pipeline, JPGs are OCR'd directly.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import datetime
from urllib.parse import urljoin

import pytesseract
import requests
from bs4 import BeautifulSoup
from PIL import Image

from .logging_utils import append_match_log, slugify
from .matcher import find_matches
from .models import Tender
from .pdf_utils import extract_text

log = logging.getLogger("mandate_bot.kppra")

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MandateBot/1.0"}


def _parse_listing_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="custom-table")
    if table is None:
        return []
    out = []
    for row in table.find_all("tr", recursive=False):
        tds = row.find_all("td", recursive=False)
        if len(tds) != 8:
            continue  # skips the hidden per-row corrigendum detail row
        action_a = tds[7].find("a")
        m = re.search(r"details\((\d+)\)", (action_a.get("onclick") if action_a else "") or "")
        if not m:
            continue
        out.append({
            "tender_id": m.group(1),
            "tender_ref": tds[0].get_text(strip=True),
            "description": tds[1].get_text(" ", strip=True),
            "department": tds[2].get_text(" ", strip=True),
            "ad_date": tds[3].get_text(strip=True),
            "close_date": tds[4].get_text(strip=True),
        })
    return out


def _fetch_detail(session: requests.Session, base_url: str, tender_id: str, verify_ssl: bool) -> dict | None:
    url = urljoin(base_url, f"includes/class.tender.php?getTenderDetails=yes&tender_id={tender_id}")
    for attempt in range(2):
        try:
            resp = session.get(url, verify=verify_ssl, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            return data[0] if data else None
        except Exception as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            log.warning("Failed to fetch tender detail %s: %s", tender_id, exc)
            return None


def fetch_all(base_url: str, listing_path: str, categories: list[str],
              verify_ssl: bool = True, request_delay: float = 0.5, max_pages: int = 20) -> list[Tender]:
    session = requests.Session()
    session.headers.update(HEADERS)
    categories_lower = {c.lower() for c in categories}

    tenders: list[Tender] = []
    page_num = 1
    while page_num <= max_pages:
        url = urljoin(base_url, f"{listing_path}?p={page_num}")
        resp = session.get(url, verify=verify_ssl, timeout=30)
        resp.raise_for_status()
        rows = _parse_listing_rows(resp.text)
        if not rows:
            break
        log.info("KPPRA page %d: %d rows", page_num, len(rows))

        for row in rows:
            detail = _fetch_detail(session, base_url, row["tender_id"], verify_ssl)
            time.sleep(request_delay)
            if not detail:
                continue
            category = detail.get("t_title") or ""
            if category.lower() not in categories_lower:
                continue

            def doc_url(filename):
                if not filename:
                    return None
                return urljoin(base_url, f"staff/force_download.php?file=dept/upload/{filename}")

            tenders.append(Tender(
                notice_type="Tender",
                title=row["description"] or detail.get("tender_descp", ""),
                category=category,
                publish_date=row["ad_date"],
                close_date=row["close_date"],
                department=row["department"],
                status="",
                notice_url=doc_url(detail.get("tender_file")),
                document_url=doc_url(detail.get("bidding_doc")) or doc_url(detail.get("tender_file")),
                source="kppra",
            ))

        page_num += 1

    return tenders


def _extract_text_any(session: requests.Session, url: str, tmp_path: str, verify_ssl: bool) -> str:
    resp = session.get(url, verify=verify_ssl, timeout=60)
    resp.raise_for_status()
    with open(tmp_path, "wb") as f:
        f.write(resp.content)

    if resp.content[:4] == b"%PDF":
        return extract_text(tmp_path)
    try:
        return pytesseract.image_to_string(Image.open(tmp_path))
    except Exception as exc:
        log.warning("Failed to OCR image %s: %s", url, exc)
        return ""


def process_candidates(candidates: list[Tender], keywords: list[str], cfg: dict, state, log_: logging.Logger) -> int:
    src_cfg = cfg.get("kppra", {})
    verify_ssl = not src_cfg.get("insecure_skip_verify", False)
    request_delay = src_cfg.get("request_delay_seconds", 0.5)

    session = requests.Session()
    session.headers.update(HEADERS)

    match_count = 0
    for t in candidates:
        # document_url falls back to the same file as notice_url when no
        # separate bidding document exists (see fetch_all) — dedupe so we
        # don't download/OCR the identical file twice.
        urls = list(dict.fromkeys(u for u in (t.document_url, t.notice_url) if u))
        if not urls:
            state.mark(t.key)
            continue

        combined_text = ""
        tmp_files = []
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                for i, url in enumerate(urls):
                    ext = ".pdf" if url.lower().endswith(".pdf") or "bidding" in url.lower() else ""
                    tmp_path = os.path.join(tmpdir, f"doc_{i}{ext or '.bin'}")
                    try:
                        text = _extract_text_any(session, url, tmp_path, verify_ssl)
                        combined_text += "\n" + text
                        tmp_files.append((url, tmp_path))
                    except Exception:
                        log_.warning("Failed to download/read %s", url)
                    time.sleep(request_delay)

                hits = find_matches(combined_text, keywords)
                if hits:
                    match_count += 1
                    folder_name = f"{t.publish_date}_{slugify(t.title)}".replace("/", "-")
                    dest_dir = os.path.join(cfg["paths"]["download_dir"], folder_name)
                    os.makedirs(dest_dir, exist_ok=True)
                    for url, tmp_path in tmp_files:
                        dest_name = os.path.basename(url.split("file=dept/upload/")[-1]) or os.path.basename(tmp_path)
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
            log_.exception("Error processing KPPRA tender %s", t.title)
            continue

        state.mark(t.key)

    return match_count
