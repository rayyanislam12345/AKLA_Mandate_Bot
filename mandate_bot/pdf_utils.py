from __future__ import annotations

import logging
import re
import time

import pdfplumber
import pytesseract
import requests
from pdf2image import convert_from_path
from PIL import Image

log = logging.getLogger("mandate_bot.pdf")

DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF = [2, 5, 10]  # seconds, one per retry attempt


def _download_with_retry(session: requests.Session, url: str, verify_ssl: bool) -> requests.Response | None:
    last_exc = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            resp = session.get(url, verify=verify_ssl, timeout=60)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < DOWNLOAD_RETRIES - 1:
                delay = DOWNLOAD_RETRY_BACKOFF[attempt]
                log.warning("Download of %s failed (attempt %d/%d): %s — retrying in %ds",
                            url, attempt + 1, DOWNLOAD_RETRIES, exc, delay)
                time.sleep(delay)
    log.warning("Failed to download %s after %d attempts: %s", url, DOWNLOAD_RETRIES, last_exc)
    return None


def download_pdf(session: requests.Session, url: str, dest_path: str, verify_ssl: bool = True) -> bool:
    """Download with retries — a network blip (connection reset, timeout)
    shouldn't cost an entire tender when it'll likely succeed a few seconds
    later. Rejects anything that isn't actually a PDF (use download_file
    instead for sources that legitimately mix PDFs/images/Word docs)."""
    resp = _download_with_retry(session, url, verify_ssl)
    if resp is None:
        return False

    content_type = resp.headers.get("Content-Type", "")
    if b"%PDF" not in resp.content[:1024] and "pdf" not in content_type.lower():
        log.warning("URL did not return a PDF, skipping: %s (content-type=%s)", url, content_type)
        return False

    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return True


def download_file(session: requests.Session, url: str, dest_path: str, verify_ssl: bool = True) -> bool:
    """Like download_pdf, but saves whatever comes back regardless of file
    type — for sources whose "documents" are legitimately a mix of PDFs,
    scanned images, and Word docs (e.g. WordPress file libraries). Pair with
    extract_text_any() to read the result."""
    resp = _download_with_retry(session, url, verify_ssl)
    if resp is None:
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


def _extract_docx_text(path: str) -> str:
    from docx import Document
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def extract_text_any(path: str) -> str:
    """Extract text from a downloaded file of unknown type: PDF (with OCR
    fallback), a Word .docx, or an image (OCR directly). Returns "" for
    anything else (e.g. .xlsx, which shares docx's zip signature but isn't
    handled) rather than raising, since a source can legitimately mix
    document types and one odd file shouldn't abort a whole run."""
    with open(path, "rb") as f:
        header = f.read(8)

    if header[:4] == b"%PDF":
        return extract_text(path)

    if header[:4] == b"PK\x03\x04":
        try:
            return _extract_docx_text(path)
        except Exception as exc:
            log.warning("Failed to extract text from %s as a Word doc: %s", path, exc)
            return ""

    try:
        return pytesseract.image_to_string(Image.open(path))
    except Exception as exc:
        log.warning("Failed to OCR %s: %s", path, exc)
        return ""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()
