#!/usr/bin/env python3
"""Head-to-head: what does the external reader tier actually buy you?

Runs the real server, over the real benchmark, in four configurations, and
compares. The question is not "is jina good" -- it clearly works -- but "is it
worth sending a third party every URL you fetch, given a renderer you host
yourself is already in the ladder".

    python scripts/ab_jina.py --crawl4ai http://127.0.0.1:11235

Each configuration gets a fresh server and a fresh data directory, so a page
cached during run one cannot flatter run two. That matters more than it sounds:
without it the second config looks twice as fast for no reason at all.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

CONFIGS = [
    ("direct only",        {"jina": "0", "crawl4ai": ""}),
    ("+ jina (pip)",       {"jina": "1", "crawl4ai": ""}),
    ("+ crawl4ai",         {"jina": "0", "crawl4ai": "URL"}),
    ("+ both",             {"jina": "1", "crawl4ai": "URL"}),
]


def wait_for(port, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % port, timeout=3):
                return True
        except Exception:                             # noqa: BLE001
            time.sleep(1)
    return False


def run_config(label, spec, args):
    data_dir = tempfile.mkdtemp(prefix="dethrottled-ab-")
    env = dict(os.environ)
    env["DETHROTTLED_DATA_DIR"] = data_dir
    env["DETHROTTLED_ENABLE_JINA"] = spec["jina"]
    env["DETHROTTLED_CRAWL4AI_URL"] = (args.crawl4ai if spec["crawl4ai"] else "")
    # The corpus would carry knowledge between configurations, which is exactly
    # the contamination a fresh data directory exists to prevent.
    env["DETHROTTLED_CORPUS_AUTOINDEX"] = "0"

    server = subprocess.Popen(
        [args.python, "-m", "dethrottled.server", "--port", str(args.port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for(args.port):
            raise SystemExit("server did not come up for %r" % label)
        out = os.path.join(data_dir, "results.json")
        subprocess.run(
            [args.python, os.path.join(HERE, "benchmark.py"),
             "--url", "http://127.0.0.1:%d" % args.port,
             "--out", out, "--top", str(args.top)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(out, encoding="utf-8") as handle:
            payload = json.load(handle)
    finally:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(data_dir, ignore_errors=True)

    summary = payload["summary"]
    tiers = {}
    for case in payload["cases"]:
        for tier in case.get("tiers") or []:
            name = (tier or "").split("/")[0]
            if name:
                tiers[name] = tiers.get(name, 0) + 1
    return summary, tiers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--crawl4ai", default="http://127.0.0.1:11235")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--top", type=int, default=3)
    args = parser.parse_args(argv)

    rows = []
    for label, spec in CONFIGS:
        print("running: %-16s ..." % label, end="", flush=True)
        started = time.time()
        summary, tiers = run_config(label, spec, args)
        print(" %ds" % int(time.time() - started))
        rows.append((label, summary, tiers))

    print()
    print("=" * 78)
    print("%-16s %8s %8s %9s %7s %9s" % (
        "config", "answered", "extract", "rate", "thin", "median ms"))
    print("-" * 78)
    for label, s, _ in rows:
        print("%-16s %6d/%-2d %5d/%-3d %8.1f%% %7d %9d" % (
            label, s["cases_with_results"], s["cases"],
            s["pages_extracted"], s["results_found"],
            s["extraction_rate"] * 100, s["thin_extractions"], s["median_ms"]))
    print("=" * 78)
    print("\nwhich tier did the work:")
    for label, _, tiers in rows:
        parts = ", ".join("%s=%d" % (k, v) for k, v in sorted(
            tiers.items(), key=lambda kv: -kv[1]))
        print("  %-16s %s" % (label, parts or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
