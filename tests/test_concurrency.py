"""Shared mutable state under threads.

FastAPI runs a sync `def` route in a threadpool, so every module-level counter,
cache and JSON file in this package is touched concurrently in production. None
of it was exercised that way until these tests existed, and two real defects
were hiding there:

  * the tier cooldown computed its backoff as `2 ** (strikes - 1)` and capped
    the result afterwards. Python integers do not overflow, so sustained
    failure built a number with hundreds of digits and raised OverflowError --
    only under heavy failure, which is when the cooldown matters most
  * engine health was an unsynchronised read-modify-write over a file written
    with truncate-then-write. Under 32 threads recording 1,280 failures the
    final count was ZERO: readers landing mid-write got invalid JSON, fell back
    to an empty dict, and wrote that back over everything

Both are fixed. These tests exist so they stay fixed.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from dethrottled import domains as dh
from dethrottled import fetch as f
from dethrottled import search as fs
from dethrottled.cache import Cache

THREADS = 16
ROUNDS = 240


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DETHROTTLED_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DETHROTTLED_ENGINE_HEALTH",
                       str(tmp_path / "engines.json"))
    monkeypatch.setattr(dh, "_state", {})
    f._tier_rest.clear()
    f._spent.clear()
    yield
    f._tier_rest.clear()
    f._spent.clear()


def hammer(fn, count=ROUNDS, workers=THREADS):
    """Run fn(i) count times across a threadpool; return any exceptions."""
    errors = []

    def guarded(i):
        try:
            fn(i)
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(guarded, range(count)))
    return errors


# ── the two defects ──────────────────────────────────────────────────────────

def test_cooldown_backoff_does_not_overflow():
    """Sustained refusal must not build an unrepresentable number."""
    errors = hammer(lambda i: f.tier_refused("crawl4ai", "crawl4ai_http_429"))
    assert not errors, "raised %r" % errors[:1]
    assert f.tier_resting("crawl4ai") is True
    assert f.tier_rest_state()["crawl4ai"]["resting_for"] <= f.TIER_COOLDOWN_MAX


def test_cooldown_backoff_stays_capped_after_many_strikes():
    for _ in range(5000):
        f.tier_refused("crawl4ai", "x")
    assert f.tier_rest_state()["crawl4ai"]["resting_for"] <= f.TIER_COOLDOWN_MAX


def test_engine_health_loses_no_updates_under_threads():
    """The read-modify-write that silently zeroed itself."""
    errors = hammer(lambda i: fs._record_failure("engine", "blocked"))
    assert not errors
    assert (fs._load_health().get("engine") or {}).get("fails") == ROUNDS


def test_engine_health_file_is_never_seen_half_written():
    """Writers rename a complete temp file into place, so a concurrent reader
    sees the old file or the new one and never a truncated one."""
    stop = threading.Event()
    corrupt = []

    def reader():
        while not stop.is_set():
            health = fs._load_health()
            if health and "engine" not in health:
                corrupt.append(health)

    watcher = threading.Thread(target=reader, daemon=True)
    watcher.start()
    try:
        hammer(lambda i: fs._record_failure("engine", "blocked"), count=120)
    finally:
        stop.set()
        watcher.join(timeout=5)
    assert not corrupt


# ── the structures that were already right ───────────────────────────────────

def test_tier_budget_never_over_grants():
    """Check-then-act: read the window, decide, append. Not atomic means more
    callers are let through than the budget allows."""
    granted = []
    lock = threading.Lock()

    def spend(_i):
        if f._spend("stress", 100):
            with lock:
                granted.append(1)

    assert not hammer(spend)
    assert len(granted) == 100


def test_domain_health_records_every_outcome():
    total = ROUNDS
    errors = hammer(lambda i: dh.record("https://x%d.example/a" % (i % 4),
                                        i % 2 == 0), count=total)
    assert not errors
    counted = sum(v["ok"] + v["fail"]
                  for v in dh.stats(limit=99)["domains"].values())
    assert round(counted) == total


def test_cache_survives_concurrent_writers(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    try:
        assert not hammer(lambda i: cache.put("search", "k%d" % (i % 8),
                                              value={"i": i}))
        assert cache.get("search", "k0") is not None
    finally:
        cache.close()


def test_cache_mixed_readers_and_writers(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    try:
        cache.put("extract", "https://x/a", value={"text": "seed"})

        def mixed(i):
            if i % 2:
                cache.put("extract", "https://x/%d" % (i % 5), value={"i": i})
            else:
                cache.get("extract", "https://x/a")

        assert not hammer(mixed)
    finally:
        cache.close()


def test_tier_cooldown_state_is_consistent_under_readers():
    def mixed(i):
        if i % 3 == 0:
            f.tier_refused("jina-reader", "jina_http_429")
        elif i % 3 == 1:
            f.tier_resting("jina-reader")
        else:
            f.tier_rest_state()

    assert not hammer(mixed)


@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("numpy") is None,
    reason="numpy not installed")
def test_corpus_matrix_rows_match_metadata_under_threads(tmp_path, monkeypatch):
    """The append optimisation's real risk: a vector and its metadata drifting
    apart returns the right score attached to the wrong document."""
    import os

    from dethrottled import corpus as c
    if not os.path.isdir(c.ENGLISH_MODEL):
        pytest.skip("embedding weights not downloaded")
    corpus = c.Corpus()
    body = "Term frequency ranking of documents in a collection. " * 8

    def mixed(i):
        corpus.add("https://c/%d" % (i % 6), "t", body)
        corpus.search("ranking documents", limit=2)

    assert not hammer(mixed, count=36, workers=8)
    matrix, meta = corpus.matrix()
    assert matrix is not None
    assert matrix.shape[0] == len(meta)
