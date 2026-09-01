#!/usr/bin/env python3
"""Can the Internet Archive stand in for a relay?

The gap left after removing the hosted reader is a narrow one: syndication
aggregators and paywalled articles, which refuse a datacentre IP and cannot be
rendered past. Fixing that needs a fetch that arrives from somewhere else.

Every obvious "somewhere else" is disqualified. A Cloudflare Worker needs an
account. Tor's exit list is published in real time and blocklisted proactively
by exactly the anti-bot vendors these sites use. Public CORS proxies are a
third party with worse reliability than the reader we just removed.

The Wayback Machine is different in kind: no account, no key, a documented API,
and it is an ARCHIVE -- so for a paywalled article it may hold a copy captured
before the paywall existed. Its weakness is equally obvious and worth measuring
rather than assuming: it only has what it has.

    python scripts/probe_wayback.py
"""
import json
import sys
import time
import urllib.parse
import urllib.request

from dethrottled import extract as fx
from dethrottled import fetch as f

# The pages that actually failed or came back partial in the parity gate.
TARGETS = [
    ("msn-1", "https://www.msn.com/en-us/news/other/europe-starts-enforcing-its-ai-act-the-worlds-biggest-tech-rulebook/ar-AA1J8Qxk"),
    ("medium-1", "https://medium.com/mlworks/why-bm25-algorithm-over-tf-idf-3a7a3e1c9a2b"),
    ("medium-2", "https://medium.com/@sany2k8dev/tf-idf-vs-bm25-in-elasticsearch-8f5b3f0d2a1e"),
    # Controls: things that already work, to check wayback is not simply worse.
    ("wikipedia", "https://en.wikipedia.org/wiki/Okapi_BM25"),
    ("hn", "https://news.ycombinator.com/"),
]

AVAILABILITY = "https://archive.org/wayback/available?url=%s"
UA = {"User-Agent": f.USER_AGENT}


def newest_snapshot(url, timeout=25):
    """Ask the availability API for the closest capture, if any."""
    request = urllib.request.Request(
        AVAILABILITY % urllib.parse.quote(url, safe=""), headers=UA)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    snap = ((body.get("archived_snapshots") or {}).get("closest") or {})
    return (snap.get("url"), snap.get("timestamp")) if snap.get("available") else (None, None)


def fetch_snapshot(snapshot_url, timeout=40):
    """Fetch the archived copy.

    `id_` after the timestamp is the important part: it asks for the ORIGINAL
    captured bytes rather than the rewritten page, so there is no injected
    toolbar, no rewritten links, and nothing of the archive's own markup for an
    extractor to mistake for content.
    """
    if "/web/" in snapshot_url and "id_/" not in snapshot_url:
        head, _, tail = snapshot_url.partition("/web/")
        stamp, _, original = tail.partition("/")
        snapshot_url = "%s/web/%sid_/%s" % (head, stamp, original)
    request = urllib.request.Request(snapshot_url, headers=UA)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(10_000_000)
    return raw.decode("utf-8", errors="replace"), snapshot_url


def main():
    print("%-12s %10s %10s %10s   %s" % (
        "page", "live", "wayback", "age(days)", "snapshot"))
    print("-" * 96)
    wins = 0
    for label, url in TARGETS:
        # what the normal ladder manages, for comparison
        try:
            result = f.fetch_and_extract(url, max_chars=50000)
            live = result["chars"] if result["ok"] else 0
        except Exception:                             # noqa: BLE001
            live = 0

        chars, age, note = 0, "-", ""
        try:
            snapshot, stamp = newest_snapshot(url)
            if not snapshot:
                note = "no capture"
            else:
                html, resolved = fetch_snapshot(snapshot)
                got = fx.extract(html, url=url, max_chars=50000)
                chars = got["chars"] if got["ok"] else 0
                if stamp:
                    from datetime import datetime, timezone
                    when = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
                        tzinfo=timezone.utc)
                    age = str((datetime.now(timezone.utc) - when).days)
                note = resolved[:44]
        except Exception as exc:                      # noqa: BLE001
            note = "%s: %s" % (type(exc).__name__, str(exc)[:30])

        if chars > live:
            wins += 1
        print("%-12s %10d %10d %10s   %s" % (label, live, chars, age, note))
        time.sleep(2.0)          # a free public archive: do not hammer it

    print("-" * 96)
    print("wayback returned more text on %d of %d pages" % (wins, len(TARGETS)))
    print("\nAge matters: an archive answers with what it captured, which for a")
    print("news page may be months stale. That is fine for a reference article")
    print("and wrong for a story about this morning.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
