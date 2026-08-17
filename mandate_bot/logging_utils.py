from __future__ import annotations

import csv
import os
import re

FIELDNAMES = ["found_at", "source", "title", "department", "category", "notice_type",
              "publish_date", "close_date", "matched_keywords", "notice_url",
              "document_url", "saved_dir"]


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
