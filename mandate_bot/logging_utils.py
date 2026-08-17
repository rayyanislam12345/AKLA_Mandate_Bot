from __future__ import annotations

import csv
import os
import re

# New fields must be appended at the end, never inserted — a run already in
# progress when this file changes keeps using its own in-memory (older)
# column list for the rest of that run, and rows are matched to the header
# positionally, so this keeps old and new rows both readable.
FIELDNAMES = ["found_at", "source", "title", "department", "category", "notice_type",
              "publish_date", "close_date", "matched_keywords", "notice_url",
              "document_url", "saved_dir", "tender_ref", "extra_urls"]


def slugify(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^\w\- ]", "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return text[:max_len] or "untitled"


def append_match_log(path: str, row: dict):
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
