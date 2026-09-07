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
skipped before ever clicking into them, so re-runs stay cheap.

Everything the dashboard shows about an opportunity except the Terms of
Reference is already on the results grid: the project title (carrying ADB's
selection number), the expertise tag, whether the package is open to a firm
or an individual, the publication date and the closing deadline. The grid's
deadline is the *effective* one — when ADB extends an advertisement, the
CSRN's own Publishing History gains an Extension row and the grid shows the
extended date — so dates are re-read on every run for already-seen rows too,
and written to the date-refresh log for the sync to patch through.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

from .logging_utils import append_match_log, unique_dest_dir, write_date_refresh
from .models import Tender

log = logging.getLogger("mandate_bot.adb")

HOME_URL = "https://selfservice.adb.org/OA_HTML/OA.jsp?OAFunc=XXCRS_CSRN_HOME_PAGE"

GOTO_RETRIES = 3
GOTO_RETRY_BACKOFF = [2, 5, 10]

# Header labels that identify the results grid. Matched case-insensitively as
# prefixes, so ADB's "Deadline\n(Manila local time)" and "Engagement Period
# (Months)" still line up.
REQUIRED_HEADERS = ("project", "expertise", "published", "deadline")


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


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _find_results_table(page):
    """OA Framework nests tables many levels deep, and an outer wrapper's
    cells repeat all the text of everything inside it — so "the first table
    whose header mentions Project and Deadline" lands on a wrapper whose
    header row is a hundred cells of filter chrome. The real grid is the
    *innermost* match: of the tables that qualify, the one with the fewest
    header cells."""
    best = None
    for table in page.query_selector_all("table"):
        rows = table.query_selector_all("tr")
        if len(rows) < 2:
            continue
        header = [_norm(c.inner_text()).lower() for c in rows[0].query_selector_all("th,td")]
        if not header or len(header) > 12:
            continue
        if all(any(h.startswith(want) for h in header) for want in REQUIRED_HEADERS):
            if best is None or len(header) < best[1]:
                best = (table, len(header))
    return best[0] if best else None


def _column_index(header: list[str]) -> dict[str, int]:
    """Maps columns by their header label rather than a fixed position, so a
    column ADB adds or reorders can't silently shift dates into the wrong
    field."""
    wanted = {
        "project": "title",
        "expertise": "expertise",
        "consultant type": "consultant_type",
        "engagement period": "months",
        "published": "published",
        "deadline": "deadline",
        "view": "view",
    }
    index: dict[str, int] = {}
    for i, cell in enumerate(header):
        low = cell.lower()
        for prefix, name in wanted.items():
            if low.startswith(prefix):
                index.setdefault(name, i)
                break
    return index


def _parse_rows(page) -> list[dict]:
    """Reads the whole results grid: title, expertise, consultant type, both
    dates, the public project link, and the row's own "View CSRN" control.

    This used to read nothing but anchor text, which is why every ADB row
    reached the dashboard with no published or closing date — and why rows
    whose title carries no parenthesised reference (ADB uses a second title
    format for some postings) were dropped entirely."""
    table = _find_results_table(page)
    if table is None:
        return []

    trs = table.query_selector_all("tr")
    header = [_norm(c.inner_text()) for c in trs[0].query_selector_all("th,td")]
    index = _column_index(header)
    if not {"title", "published", "deadline"} <= index.keys():
        return []
    widest = max(index.values())

    rows = []
    for tr in trs[1:]:
        cells = tr.query_selector_all("th,td")
        if len(cells) <= widest:
            continue  # spacer/layout row the grid interleaves between records

        def cell(name: str) -> str:
            i = index.get(name)
            return _norm(cells[i].inner_text()) if i is not None else ""

        title = cell("title")
        if not title:
            continue

        anchor = cells[index["title"]].query_selector("a")
        href = (anchor.get_attribute("href") or "") if anchor else ""
        view = cells[index["view"]].query_selector("img") if "view" in index else None

        rows.append({
            "title": title,
            "expertise": cell("expertise"),
            "consultant_type": cell("consultant_type"),
            "months": cell("months"),
            "published": cell("published"),
            "deadline": cell("deadline"),
            # The Project link goes to the public adb.org project page. Useful
            # to open, but it identifies the *project*, not this opportunity —
            # one project can advertise several packages — so it must never
            # become the dedupe key. See _make_tender.
            "project_url": href if href.startswith("http") else "",
            "view": view,
        })
    return rows


_REF_RE = re.compile(r"\(([\w./-]+)\)\s*$")


def _extract_ref(title: str) -> str | None:
    """Most titles end with a stable package reference in parens, e.g.
    "...(56146-001)" — a handful don't (a different title format ADB uses
    for some postings). When present, this is a much more reliable
    identifier than the full title text, which can drift slightly between
    two searches minutes apart against ADB's live, frequently-updated data
    (new postings shift result ordering, titles get corrected, etc.)."""
    m = _REF_RE.search(title)
    return m.group(1) if m else None


def package_key(title: str, ref: str | None) -> str:
    """The dedupe identity for one advertised package.

    ADB's E-0xxxxx-00n number identifies the *recruitment notice*, and one
    notice routinely advertises several packages — a Legal Expert and a Policy
    Consultant under the same TA appear as separate rows sharing one number.
    Keying on the number alone therefore collapsed siblings onto a single row
    and the firm never saw the others; measured on one run, 12 listed
    opportunities produced only 9 keys.

    The title is what actually names the package, so it goes in the key.
    Normalising it (case, punctuation, runs of whitespace) absorbs the small
    wording drift ADB's live data shows between runs, and the trailing
    reference is dropped because it is already carried separately.
    """
    slug = re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", _REF_RE.sub("", title).lower())).strip()
    return f"adb:{ref}|{slug}" if ref else f"adb:|{slug}"


def _make_tender(row: dict) -> Tender:
    ref = _extract_ref(row["title"])
    consultant_type = row.get("consultant_type", "")
    return Tender(
        # Firm vs Individual decides whether the firm can bid at all, so it
        # belongs on the face of the notice rather than buried in the TOR.
        notice_type=f"Consulting Opportunity ({consultant_type})" if consultant_type else "Consulting Opportunity",
        title=row["title"],
        category=row.get("expertise", ""),
        publish_date=row.get("published", ""),
        close_date=row.get("deadline", ""),
        department="Asian Development Bank",
        status=consultant_type,
        notice_url=row.get("project_url") or None,
        document_url=None,
        source="adb",
        tender_ref=ref or "",
        # Pin the identity explicitly. Left to the generic preference in
        # Tender.key it would follow notice_url now that one is populated, and
        # the ADB project page is shared by every package on the project.
        dedupe_override=package_key(row["title"], ref),
    )


def _same_opportunity(a: str, b: str) -> bool:
    """Whether two listing titles are the same advertised package.

    Comparing the reference alone is not enough: siblings under one recruitment
    notice share it, so a ref match would re-locate whichever sibling happened
    to come first and download its Terms of Reference against the wrong row.
    package_key is the identity that actually distinguishes them, and it
    normalises away the small title drift ADB's live data shows between two
    searches minutes apart."""
    return package_key(a, _extract_ref(a)) == package_key(b, _extract_ref(b))


def run(search_terms: list[str], cfg: dict, state, log_: logging.Logger) -> tuple[int, int]:
    """Combined fetch+process (see module docstring for why). No keywords
    parameter — being returned by the ADB Expertise search is itself the
    match signal here, unlike every other source. Returns
    (checked_count, match_count)."""
    checked = 0
    match_count = 0
    # Every listed opportunity's current dates, seen ones included, keyed by
    # dedupe key — this is what lets an extended deadline reach a row the bot
    # already reported.
    current_dates: dict[str, dict] = {}

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
            if not rows:
                log_.warning("ADB search %r returned no parsable rows — the results grid "
                             "may have changed shape", term)

            for row in rows:
                t = _make_tender(row)
                if t.key in seen_titles_this_run:
                    continue  # same opportunity can match multiple search terms
                seen_titles_this_run.add(t.key)

                if t.publish_date or t.close_date:
                    current_dates[t.key] = {
                        "dedupe_key": t.key,
                        "source": t.source,
                        "publish_date": t.publish_date,
                        "close_date": t.close_date,
                    }

                if state.has(t.key):
                    continue
                checked += 1

                # ADB's "Expertise" field is a tag curated by ADB staff, not
                # generic boilerplate text — being returned by the search
                # itself is the signal, so every new candidate here counts
                # as a match. The TOR is downloaded for reference/reading,
                # not as a further filter.
                try:
                    # Re-run the search so the grid (and its element handles)
                    # are in a known, valid state, then click the View control
                    # belonging to the matched row itself. The old code looked
                    # the icon up by row position in a separately-filtered
                    # list, so a single skipped row shifted every index and
                    # downloaded the wrong opportunity's TOR.
                    _search(page, term, log_)
                    fresh_row = next(
                        (r for r in _parse_rows(page) if _same_opportunity(r["title"], row["title"])),
                        None,
                    )
                    if fresh_row is None:
                        log_.warning("Could not re-locate %r after re-search, skipping", row["title"][:80])
                        continue
                    if fresh_row["view"] is None:
                        log_.warning("No View control on the row for %r, skipping", row["title"][:80])
                        continue
                    fresh_row["view"].click()
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
                    log_.info("MATCH: %s (published %s, closes %s, via search term %r)",
                              t.title, t.publish_date or "?", t.close_date or "?", term)
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
                        "notice_url": t.notice_url or "",
                        "document_url": "",
                        "saved_dir": dest_dir,
                        "tender_ref": t.tender_ref,
                        "extra_urls": "",
                        "dedupe_key": t.key,
                    })

                    state.mark(t.key)
                except Exception:
                    log_.exception("Error processing ADB opportunity %r", row["title"][:80])
                    continue

        browser.close()

    if current_dates:
        refresh_path = os.path.join(os.path.dirname(cfg["paths"]["match_log"]), "date_refresh.csv")
        write_date_refresh(refresh_path, list(current_dates.values()))
        log_.info("ADB: recorded current dates for %d listed opportunity(ies)", len(current_dates))

    return checked, match_count
