from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class CacheEntry:
    expires_at: datetime
    score: float


class RecognitionCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, CacheEntry] = {}

    def should_skip(self, camera_id: str, worker_id: int, score: float) -> bool:
        self._purge_expired()
        key = f"{camera_id}:{worker_id}"
        entry = self._entries.get(key)
        if entry is None:
            return False
        return entry.score >= score

    def remember(self, camera_id: str, worker_id: int, score: float) -> None:
        self._purge_expired()
        key = f"{camera_id}:{worker_id}"
        expires_at = datetime.utcnow() + timedelta(seconds=self.ttl_seconds)
        self._entries[key] = CacheEntry(expires_at=expires_at, score=score)

    def active_entries(self) -> int:
        self._purge_expired()
        return len(self._entries)

    def _purge_expired(self) -> None:
        now = datetime.utcnow()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            del self._entries[key]

