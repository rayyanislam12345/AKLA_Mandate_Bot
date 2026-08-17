from __future__ import annotations

import json
import os


class SeenState:
    """Tracks tender keys already processed so re-runs don't re-download/re-scan."""

    def __init__(self, path: str):
        self.path = path
        self._seen: set[str] = set()
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self._seen = set(json.load(f))

    def has(self, key: str) -> bool:
        return key in self._seen

    def mark(self, key: str):
        """Marks and immediately persists — long runs can be interrupted
        partway through, and progress up to that point should survive."""
        self._seen.add(key)
        self.save()

    def unmark(self, key: str):
        self._seen.discard(key)

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(sorted(self._seen), f, indent=2)
