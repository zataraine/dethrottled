#!/usr/bin/env python3
"""SQLite cache for the free stack.

The free stack has no quota to protect, but it does have manners and a clock.
Every cached hit is a page we do not re-request from someone else's server and
a second we do not spend, so caching is both the polite thing and the fast
thing. Search results expire quickly; fetched article bodies barely change, so
they are kept far longer.

One file, no daemon, trivially deletable — the whole lab is meant to be
removable with `rm -rf`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_TTL = {
    "search": 6 * 3600,        # a search result set goes stale fast
    # The fetcher stores bodies under "extract". This table said "fetch", so
    # every body fell through to the unnamed hourly default and 742 of the 762
    # in the cache were already dead -- meaning almost every page was refetched
    # almost every time, at nearly seven seconds each. Both spellings are here
    # now; the intent was always twenty-one days.
    "extract": 21 * 24 * 3600,
    "fetch": 21 * 24 * 3600,   # an article body essentially never changes
    "robots": 24 * 3600,
}


class Cache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Fetches run four at a time now, and a sqlite connection refuses to
        # be used from a thread other than the one that created it. WAL plus a
        # single lock is ample here: writes are small and rare, and the
        # alternative -- a connection per thread -- is four writers racing for
        # one file.
        self._db = sqlite3.connect(str(self.path), timeout=30,
                                   check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                key   TEXT PRIMARY KEY,
                kind  TEXT NOT NULL,
                value TEXT NOT NULL,
                ts    REAL NOT NULL
            )
        """)
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_kind_ts ON entries(kind, ts)")
        self._db.commit()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(kind: str, *parts) -> str:
        raw = "|".join(str(p) for p in parts)
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=16)
        return "%s:%s" % (kind, digest.hexdigest())

    def get(self, kind: str, *parts, ttl: float | None = None):
        ttl = DEFAULT_TTL.get(kind, 3600) if ttl is None else ttl
        key = self.key(kind, *parts)
        with self._lock:
            row = self._db.execute(
                "SELECT value, ts FROM entries WHERE key=?", (key,)).fetchone()
        if not row:
            self.misses += 1
            return None
        value, ts = row
        if ttl >= 0 and (time.time() - ts) > ttl:
            self.misses += 1
            return None
        self.hits += 1
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None

    def put(self, kind: str, *parts, value) -> None:
        key = self.key(kind, *parts)
        # The commit belongs inside the lock: it is part of the write, not a
        # separate thing that happens afterwards.
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO entries (key, kind, value, ts) "
                "VALUES (?,?,?,?)",
                (key, kind, json.dumps(value, ensure_ascii=False), time.time()),
            )
            self._db.commit()

    def stats(self) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, COUNT(*) FROM entries GROUP BY kind").fetchall()
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "stored": {kind: count for kind, count in rows},
            "path": str(self.path),
        }

    def purge(self, older_than_days: float = 30) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            cursor = self._db.execute("DELETE FROM entries WHERE ts < ?", (cutoff,))
            self._db.commit()
        return cursor.rowcount

    def close(self):
        try:
            self._db.close()
        except Exception:
            pass
