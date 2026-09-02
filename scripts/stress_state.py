#!/usr/bin/env python3
"""Hammer every piece of shared mutable state from many threads at once.

FastAPI runs a sync `def` route in a threadpool, so every module-level cache,
counter and JSON file in this package is touched concurrently in production and
by nothing at all in the test suite. This closes that gap the blunt way: run
each one from dozens of threads and see what survives.

Reported per structure: exceptions raised, and whether the final state is the
one arithmetic says it should be. Lost updates are the interesting failure --
they raise nothing, corrupt nothing, and quietly throw away half the record.

    python scripts/stress_state.py [--threads 32] [--rounds 50]
"""
import argparse
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor


def run(workers, fn, count):
    """Run fn(i) count times across `workers` threads; collect exceptions."""
    errors = []

    def guarded(i):
        try:
            fn(i)
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, str(exc)[:60]))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(guarded, range(count)))
    return errors


def report(name, errors, expected, actual, note=""):
    ok = not errors and expected == actual
    print("%-22s %-6s expected=%-8s actual=%-8s errors=%-3d %s"
          % (name, "PASS" if ok else "FAIL", expected, actual,
             len(errors), note))
    if errors:
        seen = {}
        for e in errors:
            seen[e] = seen.get(e, 0) + 1
        for e, n in list(seen.items())[:3]:
            print("      %dx %s" % (n, e))
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--rounds", type=int, default=50)
    args = parser.parse_args(argv)
    workers, rounds = args.threads, args.rounds

    tmp = tempfile.mkdtemp(prefix="dethrottled-stress-")
    os.environ["DETHROTTLED_DATA_DIR"] = tmp
    os.environ["DETHROTTLED_ENGINE_HEALTH"] = os.path.join(tmp, "engines.json")

    from dethrottled import domains as dh
    from dethrottled import fetch as f
    from dethrottled import search as fs
    from dethrottled.cache import Cache

    print("%d threads x %d operations\n" % (workers, rounds))
    passed = True

    # ── the cache ───────────────────────────────────────────────────────────
    cache = Cache(os.path.join(tmp, "cache.sqlite"))
    errors = run(workers, lambda i: cache.put("search", "k%d" % (i % 8),
                                              value={"i": i}), rounds * workers)
    stored = cache.stats().get("stored", {}).get("search", 0)
    passed &= report("cache writes", errors, 8, stored,
                     "8 distinct keys, written repeatedly")

    # ── tier budgets ────────────────────────────────────────────────────────
    # The classic check-then-act: read the window, decide, append. If it is not
    # atomic, more callers are granted than the budget allows.
    f._spent.clear()
    budget = 100
    granted = []
    lock = threading.Lock()

    def spend(_i):
        if f._spend("stress", budget):
            with lock:
                granted.append(1)

    errors = run(workers, spend, rounds * workers)
    passed &= report("tier budget", errors, budget, len(granted),
                     "must never over-grant")

    # ── tier cooldown ───────────────────────────────────────────────────────
    f._tier_rest.clear()
    errors = run(workers, lambda i: (f.tier_refused("crawl4ai", "x"),
                                     f.tier_resting("crawl4ai"),
                                     f.tier_rest_state()), rounds * workers)
    passed &= report("tier cooldown", errors, True,
                     f.tier_resting("crawl4ai"), "resting after N refusals")

    # ── domain health ───────────────────────────────────────────────────────
    dh.reset()
    total = rounds * workers
    errors = run(workers, lambda i: dh.record("https://x%d.example/a" % (i % 4),
                                              i % 2 == 0), total)
    dh.flush()
    counted = sum(v["ok"] + v["fail"]
                  for v in dh.stats(limit=99)["domains"].values())
    passed &= report("domain health", errors, float(total), round(counted, 1),
                     "every outcome must be recorded")

    # ── engine health (search.py) ───────────────────────────────────────────
    # Read-modify-write over a JSON file. If it is unsynchronised the count
    # comes back short, and a truncated write can lose the file entirely.
    for path in (os.environ["DETHROTTLED_ENGINE_HEALTH"],):
        if os.path.exists(path):
            os.unlink(path)
    errors = run(workers, lambda i: fs._record_failure("engine", "blocked"),
                 rounds * workers)
    fails = (fs._load_health().get("engine") or {}).get("fails", 0)
    passed &= report("engine health", errors, rounds * workers, fails,
                     "lost updates show up here")

    # ── the corpus matrix ───────────────────────────────────────────────────
    try:
        from dethrottled.corpus import Corpus
        corpus = Corpus()
        body = "Term frequency ranking of documents in a collection. " * 8
        errors = run(8, lambda i: (corpus.add("https://c/%d" % (i % 6), "t", body),
                                   corpus.search("ranking documents", limit=2)), 48)
        matrix, meta = corpus.matrix()
        rows = 0 if matrix is None else matrix.shape[0]
        passed &= report("corpus matrix", errors, len(meta), rows,
                         "vectors and metadata must stay in step")
    except Exception as exc:
        print("%-22s SKIP   %s" % ("corpus matrix", str(exc)[:50]))

    print("\n%s" % ("all shared state survived" if passed
                    else "FAILURES ABOVE -- see the expected/actual columns"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
