"""Force each fetch tier independently and print what it actually recovers.

This is the evidence behind the ladder order, and it is the reason the ladder
is judged on TEXT rather than on HTTP status. Run it and read the table: a 0
means that tier cannot solve that page at all, regardless of what status code
it returned.

    pytest tests/test_tier_matrix.py -m network -s

Marked `network` and excluded from CI, because it hits the live web and its
numbers legitimately change as sites do. It is a measurement tool that happens
to be shaped like a test, not a pass/fail gate.
"""
import time

import pytest

from dethrottled import extract as fx
from dethrottled import fetch as f

pytestmark = pytest.mark.network

# Chosen to exercise different failure modes rather than to be representative:
# a plain static page, an article layout, a page that does not exist until
# JavaScript runs, and a large reference page.
PAGES = {
    "example": "https://example.com/",
    "wikipedia": "https://en.wikipedia.org/wiki/Web_scraping",
    "quotes-js": "https://quotes.toscrape.com/js/",
    "books": "https://books.toscrape.com/",
}

TIERS = ["direct", "tls", "crawl4ai", "jina-reader"]


def run_tier(name, url):
    """Characters of usable prose this tier recovers, and how long it took."""
    started = time.time()
    try:
        if name == "direct":
            payload, reason, final = f._tier_direct(url, 20)
        elif name == "tls":
            payload, reason, final = f._tier_tls(url)
        elif name == "crawl4ai":
            payload, reason, final = f._tier_crawl4ai(url)
        else:
            payload, reason, final = f._tier_jina_reader(url)
    except Exception as exc:
        return 0, int((time.time() - started) * 1000), type(exc).__name__

    ms = int((time.time() - started) * 1000)
    payload = payload if isinstance(payload, dict) else {}

    # A tier may hand back already-extracted text; re-running a local extractor
    # over someone else's good extraction only loses content.
    supplied = payload.get("text")
    if isinstance(supplied, str) and len(supplied.strip()) > 220:
        return len(f._tidy_text(supplied)), ms, "native"

    html = payload.get("html") or ""
    if not html:
        return 0, ms, (reason or "empty")[:24]

    result = fx.extract(html, url=final, max_chars=200000)
    if not result["ok"]:
        # Fetched something, produced no prose. THIS is the state a status-code
        # ladder calls success.
        return 0, ms, "no prose (%d bytes html)" % len(html)
    return len(result["text"]), ms, result.get("extractor", "ok")


def test_tier_matrix(capsys):
    with capsys.disabled():
        print("\n%-12s %s" % ("page", "".join(t.rjust(14) for t in TIERS)))
        print("-" * (12 + 14 * len(TIERS)))
        solved = dict.fromkeys(TIERS, 0)
        unique = dict.fromkeys(TIERS, 0)

        for label, url in PAGES.items():
            chars = {}
            for tier in TIERS:
                count, ms, why = run_tier(tier, url)
                chars[tier] = count
                if count >= f.THIN_CHARS:
                    solved[tier] += 1
            winners = [t for t in TIERS if chars[t] >= f.THIN_CHARS]
            if len(winners) == 1:
                unique[winners[0]] += 1
            print("%-12s %s" % (label,
                                "".join(str(chars[t]).rjust(14) for t in TIERS)))

        print("-" * (12 + 14 * len(TIERS)))
        print("%-12s %s" % ("SOLVED",
                            "".join(str(solved[t]).rjust(14) for t in TIERS)))
        print("%-12s %s" % ("UNIQUE",
                            "".join(str(unique[t]).rjust(14) for t in TIERS)))
        print("\nA 0 means the tier cannot solve that page at all -- including"
              "\ncases where it returned HTTP 200 with a full-size body.")

    # The only hard assertion: the tier with no dependencies must work at all.
    # Everything else is a measurement, and asserting on live sites would make
    # this fail for reasons that are not about this code.
    assert solved["direct"] > 0, "the direct tier solved nothing; check network"
