from __future__ import annotations

from .pdf_utils import normalize


def find_matches(text: str, keywords: list[str]) -> list[str]:
    """Return the subset of `keywords` found in `text` (case-insensitive substring match)."""
    haystack = normalize(text)
    hits = []
    for kw in keywords:
        if normalize(kw) in haystack:
            hits.append(kw)
    return hits
