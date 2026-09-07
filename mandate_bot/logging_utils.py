from __future__ import annotations

import csv
import hashlib
import os
import re

# New fields must be appended at the end, never inserted — rows are matched to
# the header positionally, so appending keeps old and new rows both readable.
# append_match_log rewrites the header when this list grows; without that, the
# extra values would be appended under a header that never mentions them and
# csv.DictReader would file them all under the None key.
FIELDNAMES = ["found_at", "source", "title", "department", "category", "notice_type",
              "publish_date", "close_date", "matched_keywords", "notice_url",
              "document_url", "saved_dir", "tender_ref", "extra_urls", "dedupe_key"]

# A source can re-advertise an opportunity it already reported — ADB extends
# advertisement deadlines routinely — so dates seen on a listing are written
# here for every row, seen or new, and the sync patches just those two columns
# onto rows that already exist. Rewritten each run: it is a snapshot of what
# the source is advertising right now, not a history.
DATE_REFRESH_FIELDNAMES = ["dedupe_key", "source", "publish_date", "close_date"]


def slugify(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^\w\- ]", "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return text[:max_len] or "untitled"


def unique_dest_dir(download_dir: str, *parts: str, max_len: int = 70) -> str:
    """Builds a collision-resistant per-tender download folder path.
    slugify() truncates long text, so near-identical titles (e.g. sibling
    packages under one contract, differing only past the truncation point)
    can otherwise collapse to the same folder name — later downloads then
    silently overwrite earlier ones. A short hash of the untruncated input
    guarantees uniqueness while keeping the name readable."""
    raw = "_".join(p for p in parts if p)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return os.path.join(download_dir, f"{slugify(raw, max_len=max_len)}_{digest}")


def _migrate_match_log(path: str, old_header: list[str]):
    """Rewrites an existing log under the current FIELDNAMES. Values for
    columns added since the file was created come back empty, which is the
    same thing a reader would infer from a missing column anyway."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") or "" for k in FIELDNAMES})


def append_match_log(path: str, row: dict):
    is_new = not os.path.exists(path)
    if not is_new:
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), None)
        if header is not None and header != FIELDNAMES:
            _migrate_match_log(path, header)

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def write_date_refresh(path: str, rows: list[dict]):
    """Replaces the date-refresh log with this run's snapshot."""
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATE_REFRESH_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
