#!/usr/bin/env python3
"""Extraction head-to-head on a FIXED URL list.

The end-to-end benchmark measures search and extraction together, and the free
search engines return a different set of URLs on every run -- 55 results, then
60, then 55. Comparing extraction rates across different pages is not a
comparison at all, and it produced three contradictory readings before this
script existed.

So: one URL list, given to every server, /extract only. Search variance is
removed by construction and what is left is the thing actually being compared.

    python scripts/ab_extract.py --urls urls.txt \\
        --server before=http://127.0.0.1:8787 --server after=http://127.0.0.1:8788
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request


def extract(base, urls, timeout, chunk=4):
    """POST /extract in small batches, so one slow page cannot time out the lot."""
    rows = []
    for i in range(0, len(urls), chunk):
        batch = urls[i:i + chunk]
        body = json.dumps({"urls": batch, "max_chars": 8000}).encode("utf-8")
        req = urllib.request.Request(
            base.rstrip("/") + "/extract", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                got = json.load(response)
            elapsed = (time.time() - started) * 1000 / max(len(batch), 1)
            for row in got:
                row["_ms"] = elapsed
            rows.extend(got)
        except Exception as exc:
            for url in batch:
                rows.append({"url": url, "quality": "failed", "content": "",
                             "failure_reason": "client:%s" % type(exc).__name__,
                             "tier": None, "_ms": (time.time() - started) * 1000})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls", required=True, help="file, one URL per line")
    parser.add_argument("--server", action="append", required=True,
                        help="name=url, repeatable")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    with open(args.urls, encoding="utf-8") as handle:
        urls = [ln.strip() for ln in handle if ln.strip()
                and not ln.startswith("#")]
    servers = [s.split("=", 1) for s in args.server]
    print("%d urls x %d servers\n" % (len(urls), len(servers)))

    results = {}
    for name, base in servers:
        print("running %-14s ..." % name, end="", flush=True)
        started = time.time()
        results[name] = {r.get("url"): r for r in extract(base, urls, args.timeout)}
        print(" %ds" % int(time.time() - started))

    names = [n for n, _ in servers]
    print()
    print("%-52s %s" % ("url", "".join(n[:13].rjust(15) for n in names)))
    print("-" * (52 + 15 * len(names)))

    wins = dict.fromkeys(names, 0)
    solved = dict.fromkeys(names, 0)
    times = {n: [] for n in names}
    for url in urls:
        cells, sizes = [], {}
        for name in names:
            row = results[name].get(url) or {}
            chars = len(row.get("content") or "")
            ok = row.get("quality") == "ok" and chars >= 600
            solved[name] += 1 if ok else 0
            sizes[name] = chars if ok else 0
            times[name].append(row.get("_ms", 0))
            cells.append(("%d" % chars) if ok else "-")
        best = max(sizes.values())
        for name in names:
            if best and sizes[name] == best:
                wins[name] += 1
        print("%-52s %s" % (url[:52], "".join(c.rjust(15) for c in cells)))

    print("-" * (52 + 15 * len(names)))
    print("%-52s %s" % ("SOLVED (>=600 chars)",
                        "".join(("%d/%d" % (solved[n], len(urls))).rjust(15)
                                for n in names)))
    print("%-52s %s" % ("MOST TEXT",
                        "".join(str(wins[n]).rjust(15) for n in names)))
    print("%-52s %s" % ("MEDIAN ms/page",
                        "".join(str(int(statistics.median(times[n]))).rjust(15)
                                for n in names)))

    # Where they disagree is the only interesting part.
    print("\npages one solved and another did not:")
    any_diff = False
    for url in urls:
        ok = {n: (results[n].get(url) or {}).get("quality") == "ok"
              and len((results[n].get(url) or {}).get("content") or "") >= 600
              for n in names}
        if len(set(ok.values())) > 1:
            any_diff = True
            winners = [n for n in names if ok[n]]
            losers = [n for n in names if not ok[n]]
            why = (results[losers[0]].get(url) or {}).get("failure_reason", "")
            print("  %-46s %s beat %s  (%s)"
                  % (url[:46], "+".join(winners), "+".join(losers), why[:34]))
    if not any_diff:
        print("  none -- identical outcomes on every page")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"urls": urls, "results": results}, handle, indent=1)
        print("\nwrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
