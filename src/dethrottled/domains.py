"""Which domains actually yield text, learned rather than declared.

## The gap this fills

Ranking answers "is this relevant?" -- BM25 and the cross-encoder both score a
result against the query, using its title and snippet. Neither has any idea
whether the page can be READ. Those are separate questions, and a search stack
that only answers the first will confidently hand you a perfectly relevant
result that yields nothing when fetched.

Some domains reliably return nothing: single-page applications that render from
a private API, syndication aggregators, sites that serve a consent wall to
everyone. Measured, one such domain accounted for every extraction failure in a
40-page run -- nine relevant results, nine empty bodies, nine wasted fetches.

## Why this is measured and not a list

The obvious fix is a blocklist, and it is the wrong fix. A shipped list of "bad"
domains is one person's editorial opinion baked into everyone's install: it is
wrong for subjects it was not written for, it goes stale silently, and it cannot
know that a site fixed itself last month.

So nothing is declared here. This records what happened -- attempts and
successes, per domain, per deployment -- and lets the record speak. A domain
that starts working recovers on its own, because the evidence changes.

## Where it is allowed to act

Deliberately narrow. It NEVER reorders results by relevance and never removes
anything from a response. It is consulted at one point only: when the pool has
already been ranked and the service is choosing which of those results to spend
a fetch on. The pool is over-fetched precisely so there is something to choose
from.

So a caller asking for three results with content gets three that work, in
relevance order, instead of three of which one is empty.

## Cost

One small JSON file and a dictionary lookup. No model, no database, no extra
dependency -- this has to be affordable on a Raspberry Pi, where the fetch it
saves is worth far more than the microsecond it costs.
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlparse

from . import paths as _paths

# How much evidence before the record is allowed to influence anything.
#
# Not 1, and not 2. A single failure is a bad afternoon -- a timeout, a blip, a
# page that genuinely was not there -- and acting on it would make the system
# skittish in exactly the way the engine-resting logic already learned not to
# be. Five attempts is enough to tell a pattern from an accident.
MIN_ATTEMPTS = int(os.environ.get("DETHROTTLED_DOMAIN_MIN_ATTEMPTS", "5"))

# Below this success rate a domain is deprioritised for fetching. 0.2 means
# "four out of five attempts came back empty", which is a strong claim and
# deliberately hard to reach.
FLOOR = float(os.environ.get("DETHROTTLED_DOMAIN_FLOOR", "0.2"))

# Evidence gets old. Counts are halved once a fortnight so a site that was
# broken in March does not carry a grudge into June -- and so the store cannot
# grow into a permanent verdict on anything.
HALF_LIFE_DAYS = float(os.environ.get("DETHROTTLED_DOMAIN_HALF_LIFE", "14"))

ENABLED = os.environ.get("DETHROTTLED_DOMAIN_HEALTH", "1").strip() not in (
    "0", "false", "no", "off", "")

_lock = threading.Lock()
_state: dict | None = None
_dirty = False
_last_save = 0.0
SAVE_EVERY = float(os.environ.get("DETHROTTLED_DOMAIN_SAVE_SECONDS", "60"))


def domain_of(url: str) -> str:
    """The registrable-ish host. `www.` stripped so one site is one record."""
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except ValueError:
        return ""
    if ":" in host:
        host = host.split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def _path():
    return _paths.data_dir() / "domain-health.json"


def _load() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _state = {}
    return _state


def _decayed(entry: dict, now: float) -> tuple:
    """Counts as they stand today, after ageing.

    Applied on read rather than on a timer: there is no scheduler here, and a
    record nobody consults does not need to be up to date.
    """
    ok = float(entry.get("ok", 0))
    fail = float(entry.get("fail", 0))
    age_days = max(0.0, (now - float(entry.get("ts", now))) / 86400.0)
    if age_days > 0 and HALF_LIFE_DAYS > 0:
        factor = 0.5 ** (age_days / HALF_LIFE_DAYS)
        ok *= factor
        fail *= factor
    return ok, fail


def record(url: str, ok: bool) -> None:
    """Note the outcome of one fetch. Never raises; this is bookkeeping."""
    if not ENABLED:
        return
    host = domain_of(url)
    if not host:
        return
    now = time.time()
    global _dirty
    with _lock:
        state = _load()
        entry = state.get(host)
        if entry is None:
            entry = {"ok": 0.0, "fail": 0.0, "ts": now}
        else:
            # Age what is already there before adding to it, so old evidence
            # cannot be topped up indefinitely and become permanent.
            entry["ok"], entry["fail"] = _decayed(entry, now)
        entry["ok" if ok else "fail"] += 1
        entry["ts"] = now
        state[host] = entry
        _dirty = True
    _maybe_save()


def score(url: str) -> float:
    """Probability this domain yields readable text, 0.0-1.0.

    Laplace-smoothed, so a domain with one success does not score a confident
    1.0 and an unknown domain scores 0.5 rather than 0. An unseen site is not
    suspicious; it is unseen.
    """
    if not ENABLED:
        return 1.0
    host = domain_of(url)
    if not host:
        return 0.5
    with _lock:
        entry = _load().get(host)
        if not entry:
            return 0.5
        ok, fail = _decayed(entry, time.time())
    return (ok + 1.0) / (ok + fail + 2.0)


def is_poor(url: str) -> bool:
    """Enough evidence, and bad enough, to deprioritise fetching this domain.

    Both conditions are required. Without the attempt threshold a single
    unlucky page would condemn a whole site.
    """
    if not ENABLED:
        return False
    host = domain_of(url)
    if not host:
        return False
    with _lock:
        entry = _load().get(host)
        if not entry:
            return False
        ok, fail = _decayed(entry, time.time())
    if ok + fail < MIN_ATTEMPTS:
        return False
    return (ok + 1.0) / (ok + fail + 2.0) < FLOOR


def order_for_fetching(rows: list) -> list:
    """Stable reorder: known-poor domains last, everything else untouched.

    This is the ONLY place the record is allowed to change what a caller sees,
    and note what it does not do. It does not drop rows, does not rescore
    relevance, and does not reorder anything among the domains it has no
    complaint about. Rows the evidence says are unreadable move to the back of
    the queue, so that when only the first few are fetched, the fetches are
    spent on results that can actually be read.
    """
    if not ENABLED or not rows:
        return rows
    good = [r for r in rows if not is_poor(r.get("url", ""))]
    poor = [r for r in rows if is_poor(r.get("url", ""))]
    return good + poor if poor else rows


def _maybe_save() -> None:
    """Persist occasionally, not on every fetch.

    The file is a convenience across restarts, not a ledger. Writing it on
    every page would put a synchronous disk write in the fetch path for
    information that is worthless if lost.
    """
    global _dirty, _last_save
    now = time.time()
    if not _dirty or (now - _last_save) < SAVE_EVERY:
        return
    with _lock:
        state = dict(_load())
        _dirty = False
        _last_save = now
    try:
        path = _path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(path)                 # atomic: never a half-written file
    except OSError:
        pass


def flush() -> None:
    """Write now, whatever the interval. For shutdown and for tests."""
    global _last_save
    _last_save = 0.0
    _maybe_save()


def stats(limit: int = 20) -> dict:
    """What has been learned, worst first.

    Surfaced by /stats because a system that silently deprioritises part of the
    web should be able to show you exactly what it has decided and why.
    """
    if not ENABLED:
        return {"enabled": False, "domains": {}}
    now = time.time()
    with _lock:
        rows = []
        for host, entry in _load().items():
            ok, fail = _decayed(entry, now)
            if ok + fail < 0.5:           # decayed into irrelevance
                continue
            rows.append((host, ok, fail))
    rows.sort(key=lambda r: (r[1] + 1.0) / (r[1] + r[2] + 2.0))
    return {
        "enabled": True,
        "tracked": len(rows),
        "min_attempts": MIN_ATTEMPTS,
        "floor": FLOOR,
        "domains": {
            host: {"ok": round(ok, 1), "fail": round(fail, 1),
                   "score": round((ok + 1.0) / (ok + fail + 2.0), 3),
                   "deprioritised": (ok + fail) >= MIN_ATTEMPTS
                   and (ok + 1.0) / (ok + fail + 2.0) < FLOOR}
            for host, ok, fail in rows[:limit]},
    }


def reset() -> None:
    """Forget everything. Used by tests, and by anyone who wants a clean slate."""
    global _state, _dirty
    with _lock:
        _state = {}
        _dirty = True
    flush()
