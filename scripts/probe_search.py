#!/usr/bin/env python3
"""Which keyless search backends are actually worth having?

The stack this started from used three sources, two of which are NEWS feeds.
That is a real weakness: ask it a reference question and it returns whatever a
news site happened to publish about the subject, because a news feed is all it
has. This measures what each free backend actually contributes.

Four things are measured, and the third is the one that decides inclusion:

  count      results returned. Table stakes
  latency    a slow engine is one you cannot put in the hot path
  unique     domains NO other engine returned. An engine that only ever
             duplicates its neighbours earns nothing by being added
  failures   engines block self-hosted callers; an engine that dies under
             light load is worse than one that was never there

    python scripts/probe_search.py
    python scripts/probe_search.py --engines duckduckgo,brave,mojeek
"""
import argparse
import statistics
import sys
import time
from collections import defaultdict
from urllib.parse import urlparse

# Deliberately spread across the kinds of question a search layer gets asked.
# A query set of all news, or all reference, measures one engine's speciality
# rather than its usefulness.
QUERIES = [
    ("reference",   "okapi bm25 ranking function"),
    ("reference",   "http status code 429 meaning"),
    ("technical",   "python asyncio semaphore deadlock"),
    ("technical",   "sqlite wal mode concurrent writers"),
    ("news",        "latest EU AI Act enforcement"),
    ("news",        "semiconductor export controls"),
    ("product",     "raspberry pi 4b power consumption"),
    ("howto",       "how to configure nginx reverse proxy"),
    ("longtail",    "trafilatura vs resiliparse extraction quality"),
    ("multiling",   "recette tajine poulet citron"),
    ("multiling",   "Mietpreisbremse Berlin Regeln"),
    ("local",       "opening hours british library london"),
]

ENGINES = ["duckduckgo", "google", "bing", "brave", "mojeek",
           "startpage", "yahoo", "wikipedia"]


def domain(url):
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def run_ddgs(engine, query, max_results):
    from ddgs import DDGS
    rows = DDGS().text(query, backend=engine, max_results=max_results)
    return [r.get("href") or r.get("url") or "" for r in rows]


def run_current_stack(_engine, query, max_results):
    """What the package does today: Bing News RSS + Google News RSS.

    SearXNG is excluded because it is not configured here, which is exactly the
    situation a plain `pip install` is in -- and therefore the honest baseline.
    """
    from dethrottled import search as fs
    rows = fs.bing_news(query, max_items=max_results)
    try:
        rows += fs.google_news_headlines(query, max_items=max_results)
    except Exception:
        pass
    return [r.get("url", "") for r in rows]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engines", default=",".join(ENGINES))
    parser.add_argument("--max-results", type=int, default=8)
    parser.add_argument("--gap", type=float, default=1.5,
                        help="seconds between requests to one engine")
    args = parser.parse_args(argv)

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    engines.append("CURRENT(rss)")

    counts = defaultdict(list)
    times = defaultdict(list)
    fails = defaultdict(int)
    # per query: engine -> set of domains, so uniqueness is judged per question
    per_query = []

    for category, query in QUERIES:
        print("  %-10s %s" % (category, query[:52]))
        found = {}
        for engine in engines:
            runner = run_current_stack if engine == "CURRENT(rss)" else run_ddgs
            started = time.time()
            try:
                urls = runner(engine, query, args.max_results)
                urls = [u for u in urls if u]
            except Exception:
                fails[engine] += 1
                urls = []
            times[engine].append((time.time() - started) * 1000)
            counts[engine].append(len(urls))
            found[engine] = {domain(u) for u in urls if domain(u)}
            time.sleep(args.gap)
        per_query.append(found)

    # A domain is "unique" to an engine when no other engine returned it for
    # that same query. Counted per query and summed, never across the whole run
    # -- otherwise an engine looks unique merely for being asked a question the
    # others were not.
    unique = defaultdict(int)
    for found in per_query:
        for engine, domains in found.items():
            others = set().union(*[d for e, d in found.items() if e != engine]) \
                if len(found) > 1 else set()
            unique[engine] += len(domains - others)

    print()
    print("=" * 78)
    print("%-15s %8s %8s %9s %9s %8s" % (
        "engine", "results", "median", "unique", "queries", "fails"))
    print("%-15s %8s %8s %9s %9s %8s" % (
        "", "total", "ms", "domains", "answered", ""))
    print("-" * 78)
    ranked = sorted(engines, key=lambda e: -sum(counts[e]))
    for engine in ranked:
        answered = sum(1 for c in counts[engine] if c)
        print("%-15s %8d %8d %9d %6d/%-2d %8d" % (
            engine, sum(counts[engine]),
            int(statistics.median(times[engine])) if times[engine] else 0,
            unique[engine], answered, len(QUERIES), fails[engine]))
    print("=" * 78)
    print("\nunique = domains no other engine returned for the same query.")
    print("An engine with results but ~0 unique adds nothing you did not have.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
