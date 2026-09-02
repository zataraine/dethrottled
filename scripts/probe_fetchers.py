#!/usr/bin/env python3
"""Head-to-head: which fetch strategy actually recovers article text?

Not "which returns 200" -- that is the question this whole project exists to
stop asking. Every strategy below is scored on characters of readable prose
after extraction, which is the only number that means anything downstream.

    python scripts/probe_fetchers.py
    python scripts/probe_fetchers.py --strategies direct,curl_cffi
"""
import argparse
import statistics
import sys
import time

from dethrottled import extract as fx
from dethrottled import fetch as f

# Deliberately hard, and each one hard in a DIFFERENT way. A URL set where
# everything is a plain article measures nothing.
PAGES = [
    ("static-simple",   "https://example.com/"),
    ("wikipedia",       "https://en.wikipedia.org/wiki/Web_scraping"),
    ("js-only",         "https://quotes.toscrape.com/js/"),
    ("scrape-sandbox",  "https://books.toscrape.com/"),
    ("news-bbc",        "https://www.bbc.com/news"),
    ("news-guardian",   "https://www.theguardian.com/international"),
    ("hn",              "https://news.ycombinator.com/"),
    ("gov-uk",          "https://www.gov.uk/browse/visas-immigration"),
    ("arxiv-abs",       "https://arxiv.org/abs/2005.11401"),
    ("cloudflare-docs", "https://developers.cloudflare.com/workers/"),
    ("reddit",          "https://www.reddit.com/r/programming/"),
    ("stackoverflow",   "https://stackoverflow.com/questions/tagged/python"),
    ("github-readme",   "https://github.com/psf/requests"),
    ("mdn",             "https://developer.mozilla.org/en-US/docs/Web/HTTP"),
    ("indeed",          "https://www.indeed.com/"),
    ("g2",              "https://www.g2.com/categories/crm"),
]

TIMEOUT = 25


# ── strategies ───────────────────────────────────────────────────────────────

def s_direct(url):
    """What the package does today: requests, honest UA, robots respected."""
    payload, reason, final = f._tier_direct(url, TIMEOUT)
    return payload, reason, final


def s_curl_cffi(url, impersonate="chrome"):
    """curl_cffi: a real Chrome TLS/JA3 fingerprint, no browser.

    The interesting question. A great deal of "bot detection" is not looking at
    your User-Agent at all -- it is looking at the shape of your TLS handshake,
    which every Python HTTP client gets conspicuously wrong. If that is what is
    blocking us, this fixes it for the price of one dependency and no browser.
    """
    from curl_cffi import requests as creq
    response = creq.get(url, impersonate=impersonate, timeout=TIMEOUT,
                        allow_redirects=True)
    if response.status_code >= 400:
        return {}, "http_%d" % response.status_code, str(response.url)
    return {"html": response.text}, "", str(response.url)


def s_jina(url):
    """The external reader. Free, keyless, and it sees every URL you send."""
    saved = f.ENABLE_JINA
    f.ENABLE_JINA = True
    try:
        return f._tier_jina_reader(url)
    finally:
        f.ENABLE_JINA = saved


def s_crawl4ai(url):
    """Headless Chromium, if one is configured."""
    return f._tier_crawl4ai(url)


STRATEGIES = {
    "direct": s_direct,
    "curl_cffi": s_curl_cffi,
    "jina": s_jina,
    "crawl4ai": s_crawl4ai,
}


def prose(payload, reason, final, url):
    """Characters of readable article text, however the payload arrived."""
    payload = payload if isinstance(payload, dict) else {}
    supplied = payload.get("text")
    if isinstance(supplied, str) and len(supplied.strip()) > 220:
        return len(f._tidy_text(supplied)), "native"
    html = payload.get("html") or ""
    if not html:
        return 0, (reason or "empty")[:22]
    result = fx.extract(html, url=final or url, max_chars=500000)
    if not result["ok"]:
        # Fetched something, extracted nothing. The empty-shell case.
        return 0, "shell:%dkb" % (len(html) // 1024)
    return len(result["text"]), result.get("extractor", "ok")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategies", default="direct,curl_cffi,jina")
    parser.add_argument("--impersonate", default="chrome")
    args = parser.parse_args(argv)
    names = [n.strip() for n in args.strategies.split(",") if n.strip()]

    print("characters of PROSE recovered (0 = tier cannot solve this page)\n")
    print("%-17s %s" % ("page", "".join(n.rjust(13) for n in names)))
    print("-" * (17 + 13 * len(names)))

    solved = dict.fromkeys(names, 0)
    unique = dict.fromkeys(names, 0)
    times = {n: [] for n in names}
    notes = {}

    for label, url in PAGES:
        row = {}
        for name in names:
            started = time.time()
            try:
                fn = STRATEGIES[name]
                out = (fn(url, args.impersonate) if name == "curl_cffi"
                       else fn(url))
                chars, why = prose(*out, url)
            except Exception as exc:
                chars, why = 0, type(exc).__name__[:22]
            times[name].append(int((time.time() - started) * 1000))
            row[name] = chars
            if chars >= f.THIN_CHARS:
                solved[name] += 1
            else:
                notes.setdefault(label, {})[name] = why
        winners = [n for n in names if row[n] >= f.THIN_CHARS]
        if len(winners) == 1:
            unique[winners[0]] += 1
        mark = " *" if len(winners) == 1 else ""
        print("%-17s %s%s" % (label,
                              "".join(str(row[n]).rjust(13) for n in names), mark))

    print("-" * (17 + 13 * len(names)))
    print("%-17s %s" % ("SOLVED",
                        "".join(("%d/%d" % (solved[n], len(PAGES))).rjust(13)
                                for n in names)))
    print("%-17s %s" % ("UNIQUE",
                        "".join(str(unique[n]).rjust(13) for n in names)))
    print("%-17s %s" % ("MEDIAN ms",
                        "".join(str(int(statistics.median(times[n]))).rjust(13)
                                for n in names)))
    print("\n* = only one strategy solved that page")

    if notes:
        print("\nwhy each miss missed:")
        for label in sorted(notes):
            for name, why in notes[label].items():
                print("  %-17s %-11s %s" % (label, name, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
