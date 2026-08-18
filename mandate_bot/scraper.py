from __future__ import annotations

import logging
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .aspnet_form import build_postback_payload
from .http_utils import get_with_retry, post_with_retry
from .models import Tender

log = logging.getLogger("mandate_bot.scraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MandateBot/1.0 (+contact: bot maintainer)",
}

GRID_TABLE_CLASS = "rgMasterTable"
# Column order confirmed from the live page header row (2026-08-15):
# Procurement Title | Procurement Name | Type | Publish Date | Close Date |
# Department | Status | Tender Notice | Bidding Document
COL_NOTICE_TYPE, COL_TITLE, COL_CATEGORY, COL_PUBLISH, COL_CLOSE, COL_DEPT, COL_STATUS, COL_NOTICE_LINK, COL_DOC_LINK = range(9)


class TenderScraper:
    def __init__(self, base_url: str, listing_path: str, verify_ssl: bool = True,
                 request_delay: float = 1.5, max_pages: int = 20):
        self.base_url = base_url
        self.listing_url = urljoin(base_url, listing_path)
        self.verify_ssl = verify_ssl
        self.request_delay = request_delay
        self.max_pages = max_pages
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _get(self, url: str) -> BeautifulSoup:
        resp = get_with_retry(self.session, url, log, verify=self.verify_ssl, timeout=30)
        return BeautifulSoup(resp.text, "lxml")

    def _post(self, url: str, payload: dict) -> BeautifulSoup:
        resp = post_with_retry(self.session, url, log, data=payload, verify=self.verify_ssl, timeout=30)
        return BeautifulSoup(resp.text, "lxml")

    def _parse_rows(self, soup: BeautifulSoup) -> list[Tender]:
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
                    return urljoin(self.listing_url, a["href"])
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

    def _next_page_target(self, soup: BeautifulSoup, current_page: int) -> str | None:
        """Find the __doPostBack target for `current_page + 1` in the numeric pager."""
        pager = soup.find("div", class_="rgNumPart")
        if pager is None:
            return None
        target_label = str(current_page + 1)
        for a in pager.find_all("a"):
            span = a.find("span")
            label = span.get_text(strip=True) if span else a.get_text(strip=True)
            if label == target_label:
                href = a.get("href", "")
                # href looks like: javascript:__doPostBack('ctl00$...$ctl07','')
                if "__doPostBack(" in href:
                    inner = href.split("__doPostBack(", 1)[1].rstrip(")")
                    parts = [p.strip().strip("'") for p in inner.split(",")]
                    return parts[0]
        return None

    def fetch_all(self) -> list[Tender]:
        log.info("Fetching page 1: %s", self.listing_url)
        soup = self._get(self.listing_url)
        all_tenders = self._parse_rows(soup)
        log.info("Page 1: %d rows", len(all_tenders))

        page = 1
        while page < self.max_pages:
            target = self._next_page_target(soup, page)
            if not target:
                break
            time.sleep(self.request_delay)
            payload = build_postback_payload(soup, target)
            log.info("Fetching page %d via postback %s", page + 1, target)
            soup = self._post(self.listing_url, payload)
            rows = self._parse_rows(soup)
            if not rows:
                break
            all_tenders.extend(rows)
            log.info("Page %d: %d rows (total %d)", page + 1, len(rows), len(all_tenders))
            page += 1

        return all_tenders
