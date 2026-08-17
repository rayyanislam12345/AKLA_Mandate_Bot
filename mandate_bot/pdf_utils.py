from __future__ import annotations

import logging
import re
import time

import pdfplumber
import pytesseract
import requests
from pdf2image import convert_from_path

log = logging.getLogger("mandate_bot.pdf")

DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF = [2, 5, 10]  # seconds, one per retry attempt


def download_pdf(session: requests.Session, url: str, dest_path: str, verify_ssl: bool = True) -> bool:
    """Download with retries — a network blip (connection reset, timeout)
    shouldn't cost an entire tender when it'll likely succeed a few seconds
    later."""
    last_exc = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            resp = session.get(url, verify=verify_ssl, timeout=60)
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < DOWNLOAD_RETRIES - 1:
                delay = DOWNLOAD_RETRY_BACKOFF[attempt]
                log.warning("Download of %s failed (attempt %d/%d): %s — retrying in %ds",
                            url, attempt + 1, DOWNLOAD_RETRIES, exc, delay)
                time.sleep(delay)
    else:
        log.warning("Failed to download %s after %d attempts: %s", url, DOWNLOAD_RETRIES, last_exc)
        return False

    content_type = resp.headers.get("Content-Type", "")
    if b"%PDF" not in resp.content[:1024] and "pdf" not in content_type.lower():
        log.warning("URL did not return a PDF, skipping: %s (content-type=%s)", url, content_type)
        return False

    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return True


MIN_CHARS_TO_SKIP_OCR = 20  # pages with less embedded text than this are treated as scanned/image-only


def extract_text(pdf_path: str, ocr_dpi: int = 200) -> str:
    """Extract text from every page of a PDF, page by page: use the embedded
    text layer where present, and fall back to OCR only for pages that have
    little/no extractable text (i.e. scanned or image-only pages). This
    portal mixes native and scanned PDFs, so this hybrid keeps normal digital
    documents fast while still catching scanned ones."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = pdf.pages
            page_texts = [(p.extract_text() or "") for p in pages]
    except Exception as exc:
        log.warning("Failed to open %s for text extraction: %s", pdf_path, exc)
        page_texts = []

    needs_ocr = [i for i, t in enumerate(page_texts) if len(t.strip()) < MIN_CHARS_TO_SKIP_OCR]

    if needs_ocr:
        try:
            images = convert_from_path(pdf_path, dpi=ocr_dpi)
            for i in needs_ocr:
                if i < len(images):
                    page_texts[i] = pytesseract.image_to_string(images[i])
        except Exception as exc:
            log.warning("OCR failed for %s: %s", pdf_path, exc)

    return "\n".join(page_texts)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()
