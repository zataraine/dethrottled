#!/usr/bin/env python3
"""Functional health: capability, not reachability.

Written after two failures that a conventional health check could not see, and
which cost days each:

  * a headless-render service returned `{"status":"ok"}` for three and a half
    days while every single render silently stalled behind an exhausted
    semaphore
  * a hosted reader API reported a routine `daily_cap` for three days while
    actually running on a revoked key -- the same message for "come back
    tomorrow" and for "you will never work again"

Both were invisible because the checks asked "are you up?" instead of "can you
do the job?". A process answering /health proves a socket is open. It proves
nothing whatsoever about whether the thing behind it still works.

So every probe here does real work and inspects the result:

  * each fetch tier is asked to retrieve a page it is specifically good at
  * each search source is asked for results, and the rows are counted
  * a tier that answers but returns nothing usable is reported DEGRADED

DEGRADED is the important state. It is the one that hid both faults above:
HTTP 200, a response body, and not one word of article text in it.

OFF is the second important state, and the reason this exits 0 on a minimal
install. A tier nobody configured has not failed -- reporting it as DEAD would
mean a perfectly healthy `pip install` fails its own health check, and a check
that cries wolf on a working system is a check people learn to ignore.

Run:  python -m dethrottled.health [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import extract as fx
from . import fetch as fetcher
from . import search as fs

# Targets chosen so each tier is exercised on the property it exists for, and
# durable enough that a probe failing means the TIER failed rather than that a
# news article aged out. Override any with DETHROTTLED_PROBE_<TIER>.
#
#   direct    a plain static page any HTTP client should manage
#   tls       a host that refuses an ordinary Python TLS fingerprint
#   crawl4ai  a page whose content does not exist until JavaScript has run
PROBES = {
    "direct": os.environ.get(
        "DETHROTTLED_PROBE_DIRECT", "https://en.wikipedia.org/wiki/Web_scraping"),
    "tls": os.environ.get(
        "DETHROTTLED_PROBE_TLS", "https://www.indeed.com/"),
    "crawl4ai": os.environ.get(
        "DETHROTTLED_PROBE_CRAWL4AI", "https://quotes.toscrape.com/js/"),
    "jina-reader": os.environ.get(
        "DETHROTTLED_PROBE_JINA_READER",
        "https://en.wikipedia.org/wiki/Web_scraping"),
}


def _configured(name: str) -> bool:
    """Is this tier switched on at all?

    Kept separate from whether it WORKS, because conflating the two is how a
    health check ends up reporting a fault that is really a setting.
    """
    if name == "crawl4ai":
        return bool(fetcher.CRAWL4AI_URL)
    if name == "jina-reader":
        return bool(fetcher.ENABLE_JINA)
    if name == "tls":
        return bool(fetcher.ENABLE_TLS)
    return True


def probe_tier(name: str, url: str) -> dict:
    """Force one specific tier and check it produces usable prose."""
    if not _configured(name):
        return {"tier": name, "status": "OFF", "chars": 0,
                "detail": "not configured", "ms": 0}

    runners = {
        "direct": lambda: fetcher._tier_direct(url, 20),
        "tls": lambda: fetcher._tier_tls(url),
        "crawl4ai": lambda: fetcher._tier_crawl4ai(url),
        "jina-reader": lambda: fetcher._tier_jina_reader(url),
    }
    if name not in runners:
        return {"tier": name, "status": "OFF", "chars": 0,
                "detail": "no probe defined", "ms": 0}

    started = time.time()
    try:
        payload, reason, final = runners[name]()
    except Exception as exc:
        return {"tier": name, "status": "DEAD", "chars": 0,
                "detail": type(exc).__name__,
                "ms": int((time.time() - started) * 1000)}

    payload = payload if isinstance(payload, dict) else {}
    ms = int((time.time() - started) * 1000)

    text = payload.get("text")
    if isinstance(text, str) and len(text.strip()) > 220:
        chars = len(fetcher._tidy_text(text))
        return {"tier": name,
                "status": "OK" if chars >= fetcher.THIN_CHARS else "THIN",
                "chars": chars, "detail": "native extraction", "ms": ms}

    html = payload.get("html") or ""
    if not html:
        # A tier that declined because it is unconfigured or resting is not
        # broken, and saying so sends the reader to the right place.
        quiet = ("not_configured", "disabled", "resting", "budget_spent")
        if any(word in (reason or "") for word in quiet):
            return {"tier": name, "status": "OFF", "chars": 0,
                    "detail": reason, "ms": ms}
        return {"tier": name, "status": "DEAD", "chars": 0,
                "detail": reason or "no payload", "ms": ms}

    result = fx.extract(html, url=final, max_chars=30000)
    if not result["ok"]:
        # Fetched something, produced no prose. This is the state that looks
        # healthy to a naive check and is worthless in practice.
        return {"tier": name, "status": "DEGRADED", "chars": 0,
                "detail": "fetched %d bytes, no prose" % len(html), "ms": ms}
    return {"tier": name,
            "status": "OK" if result["chars"] >= fetcher.THIN_CHARS else "THIN",
            "chars": result["chars"], "detail": result["tier"], "ms": ms}


def probe_search() -> list:
    """Ask every configured source for real results and count what comes back.

    The query is deliberately dull and evergreen: a probe that returns nothing
    in a quiet week is a probe that reports a healthy stack as broken.
    """
    query = fs.PROBE_QUERY
    sources = [
        ("web", lambda: fs.web_search(query, max_items=6), bool(fs.WEB_ENGINES)),
        ("searxng", lambda: fs.searxng(query, max_items=6), bool(fs.SEARXNG_URL)),
        ("bing-news", lambda: fs.bing_news(query, max_items=6), True),
        ("google-news", lambda: fs.google_news_headlines(query, max_items=6), True),
    ]

    rows = []
    for name, fn, configured in sources:
        if not configured:
            rows.append({"source": name, "status": "OFF", "count": 0,
                         "detail": "not configured", "ms": 0})
            continue
        started = time.time()
        try:
            found = fn() or []
            ms = int((time.time() - started) * 1000)
            resolvable = sum(1 for r in found
                             if isinstance(r, dict) and r.get("url"))
            if not found:
                rows.append({"source": name, "status": "DEAD", "count": 0,
                             "detail": "no results", "ms": ms})
            elif name != "google-news" and not resolvable:
                # Results carrying no usable URL are the search equivalent of a
                # fetched page with no prose in it.
                rows.append({"source": name, "status": "DEGRADED",
                             "count": len(found),
                             "detail": "results carry no usable URL", "ms": ms})
            else:
                detail = ("headlines only, resolved via bing-news"
                          if name == "google-news" else "%d resolvable" % resolvable)
                rows.append({"source": name, "status": "OK", "count": len(found),
                             "detail": detail, "ms": ms})
        except Exception as exc:
            rows.append({"source": name, "status": "DEAD", "count": 0,
                         "detail": type(exc).__name__,
                         "ms": int((time.time() - started) * 1000)})
    return rows


def verdict(tiers: list, searches: list) -> tuple:
    """Exit status, and why.

    Two rules, and both are about not crying wolf:

      * a tier that is OFF is a smaller stack, not a broken one
      * one dead tier is survivable, because the ladder has others. Losing
        EVERY search source is not survivable, because nothing downstream has
        anything to work on
    """
    live_search = [r for r in searches if r["status"] != "OFF"]
    dead_search = [r for r in live_search if r["status"] == "DEAD"]
    dead_tiers = [r for r in tiers if r["status"] == "DEAD"]
    degraded = [r for r in tiers + searches if r["status"] == "DEGRADED"]

    if live_search and len(dead_search) == len(live_search):
        return 2, "CRITICAL: every configured search source is dead"
    if not [r for r in tiers if r["status"] in ("OK", "THIN")]:
        return 2, "CRITICAL: no fetch tier can retrieve a page"
    if dead_tiers or degraded:
        return 1, "degraded: %d dead, %d degraded" % (len(dead_tiers), len(degraded))
    return 0, "all configured components working"


def main() -> int:
    parser = argparse.ArgumentParser(description="Functional health probes.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    fetcher.reset_tier_stats()
    tiers = [probe_tier(name, url) for name, url in PROBES.items()]
    searches = probe_search()
    code, summary = verdict(tiers, searches)

    if args.json:
        print(json.dumps({"tiers": tiers, "search": searches,
                          "verdict": summary, "exit": code}, indent=2))
        return code

    print("DETHROTTLED FUNCTIONAL HEALTH  -  %s"
          % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("Every probe does real work. A tier that answers but returns no")
    print("usable prose is DEGRADED, not OK. A tier nobody configured is OFF.")
    print()
    print("FETCH TIERS")
    print("  %-12s %-9s %8s %8s  %s" % ("tier", "status", "chars", "ms", "detail"))
    print("  " + "-" * 68)
    for row in tiers:
        print("  %-12s %-9s %8d %8d  %s"
              % (row["tier"], row["status"], row["chars"], row["ms"],
                 row["detail"][:32]))
    print()
    print("SEARCH SOURCES")
    print("  %-13s %-9s %7s %8s  %s" % ("source", "status", "count", "ms", "detail"))
    print("  " + "-" * 68)
    for row in searches:
        print("  %-13s %-9s %7d %8d  %s"
              % (row["source"], row["status"], row["count"], row["ms"],
                 row["detail"][:32]))
    print()
    print("  %s" % summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
