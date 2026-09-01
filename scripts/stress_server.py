#!/usr/bin/env python3
"""Drive the running server from many clients at once.

The routes are sync `def`, so FastAPI runs them in a threadpool -- anyio's,
which defaults to 40 workers. That is a real ceiling and it had never been
touched: every measurement of this service so far has been one request at a
time.

What this looks for is not throughput. It is:

  * requests that fail only when others are in flight
  * latency that collapses rather than degrades, which is what hitting the
    threadpool ceiling looks like from outside
  * responses that come back wrong rather than slow, which is what shared
    state without a lock looks like

    python scripts/stress_server.py --url http://127.0.0.1:8787 --clients 16
"""
import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

URLS = [
    "https://en.wikipedia.org/wiki/Web_scraping",
    "https://en.wikipedia.org/wiki/Okapi_BM25",
    "https://news.ycombinator.com/",
    "https://example.com/",
]


def call(base, path, payload, timeout=180):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + path, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.load(response)
        return True, (time.time() - started) * 1000, parsed, ""
    except urllib.error.HTTPError as exc:
        return False, (time.time() - started) * 1000, None, "HTTP %d" % exc.code
    except Exception as exc:                          # noqa: BLE001
        return False, (time.time() - started) * 1000, None, type(exc).__name__


def get(base, path, timeout=60):
    started = time.time()
    try:
        with urllib.request.urlopen(base.rstrip("/") + path,
                                    timeout=timeout) as response:
            return True, (time.time() - started) * 1000, json.load(response), ""
    except Exception as exc:                          # noqa: BLE001
        return False, (time.time() - started) * 1000, None, type(exc).__name__


def phase(name, base, make_job, count, clients):
    """Run one workload at a given concurrency and report the distribution."""
    started = time.time()
    with ThreadPoolExecutor(max_workers=clients) as pool:
        results = list(pool.map(lambda i: make_job(base, i), range(count)))
    wall = time.time() - started

    oks = [r for r in results if r[0]]
    times = sorted(r[1] for r in results)
    errors = {}
    for r in results:
        if not r[0]:
            errors[r[3]] = errors.get(r[3], 0) + 1

    print("  %-22s %3d/%-3d ok  p50 %6.0fms  p95 %7.0fms  wall %5.1fs  %s"
          % (name, len(oks), count, statistics.median(times),
             times[int(len(times) * 0.95) - 1], wall,
             ("errors: %s" % errors) if errors else ""))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--rounds", type=int, default=4)
    args = parser.parse_args(argv)
    base = args.url

    ok, _, body, err = get(base, "/health")
    if not ok:
        print("server not reachable at %s (%s)" % (base, err))
        return 1
    print("server: %s\n" % body.get("service"))

    print("SEQUENTIAL BASELINE (1 client)")
    phase("/health", base, lambda b, i: get(b, "/health"), 8, 1)
    phase("/fetch cached", base,
          lambda b, i: call(b, "/fetch", {"urls": [URLS[0]]}), 4, 1)

    print("\nCONCURRENT (%d clients)" % args.clients)
    count = args.clients * args.rounds

    phase("/health", base, lambda b, i: get(b, "/health"), count, args.clients)
    phase("/v2/capabilities", base,
          lambda b, i: get(b, "/v2/capabilities"), count, args.clients)
    # Same four URLs repeatedly: this is the cache and the domain-health
    # recorder being hit from every thread at once.
    fetches = phase("/fetch same urls", base,
                    lambda b, i: call(b, "/fetch",
                                      {"urls": [URLS[i % len(URLS)]]}),
                    count, args.clients)
    phase("/stats", base, lambda b, i: get(b, "/stats"), count, args.clients)

    print("\nMIXED WORKLOAD (%d clients)" % args.clients)

    def mixed(b, i):
        if i % 4 == 0:
            return get(b, "/stats")
        if i % 4 == 1:
            return get(b, "/v2/status")
        if i % 4 == 2:
            return call(b, "/fetch", {"urls": [URLS[i % len(URLS)]]})
        return call(b, "/search", {"query": "okapi bm25 ranking",
                                   "num_results": 3})

    phase("mixed", base, mixed, count, args.clients)

    # Correctness under load matters more than speed: a response that comes
    # back wrong is worse than one that comes back slowly.
    print("\nCORRECTNESS UNDER LOAD")
    bad = []
    for ok, _ms, parsed, _err in fetches:
        if not ok or not isinstance(parsed, list) or not parsed:
            continue
        row = parsed[0]
        if row.get("quality") == "ok" and not (row.get("content") or "").strip():
            bad.append("ok row with empty content")
        if row.get("url") and row["url"] not in URLS:
            bad.append("response carried a url nobody asked for: %s" % row["url"])
    print("  %s" % ("no mismatched or empty responses"
                    if not bad else "PROBLEMS: %s" % set(bad)))

    ok, _, stats, _ = get(base, "/stats")
    if ok:
        print("  tiers resting: %s" % (stats.get("tiers_resting") or "none"))
        tracked = (stats.get("domain_health") or {}).get("tracked", 0)
        print("  domains tracked: %s" % tracked)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
