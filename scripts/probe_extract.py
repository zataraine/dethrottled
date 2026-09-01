#!/usr/bin/env python3
"""Which extractor should be in the cascade, and in what order?

Quality AND speed, because this stack is meant to run on a Raspberry Pi as well
as a workstation, and an extractor that is 30x slower is a different product on
a Pi 4B even when its output is identical.

Scored on real pages fetched once and reused, so every extractor sees exactly
the same bytes. Quality here is not "characters recovered" -- more text is not
better text, and the easiest way to win that metric is to return the navigation
menu. Two numbers instead:

  chars      what it returned
  junk       how much of it is boilerplate. Measured by looking for phrases
             that only ever appear in page furniture: cookie banners, share
             buttons, subscribe prompts, copyright lines

    python scripts/probe_extract.py
"""
import re
import statistics
import sys
import time

from dethrottled import fetch as f

PAGES = [
    ("wikipedia",     "https://en.wikipedia.org/wiki/Web_scraping"),
    ("mdn",           "https://developer.mozilla.org/en-US/docs/Web/HTTP"),
    ("bbc",           "https://www.bbc.com/news"),
    ("guardian",      "https://www.theguardian.com/international"),
    ("hn",            "https://news.ycombinator.com/"),
    ("arxiv",         "https://arxiv.org/abs/2005.11401"),
    ("cf-docs",       "https://developers.cloudflare.com/workers/"),
    ("github",        "https://github.com/psf/requests"),
    ("gov-uk",        "https://www.gov.uk/browse/visas-immigration"),
    ("books",         "https://books.toscrape.com/"),
]

# Phrases that appear in furniture and effectively never in article prose.
JUNK = re.compile(
    r"cookie|subscribe|newsletter|sign in|sign up|all rights reserved|"
    r"privacy policy|terms of service|follow us|share this|advertisement|"
    r"accept all|manage preferences|skip to (main )?content",
    re.I)


def junk_ratio(text):
    """Roughly what fraction of the lines look like page furniture."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return 0.0
    return sum(1 for ln in lines if JUNK.search(ln)) / len(lines)


# ── the candidates ───────────────────────────────────────────────────────────

def x_trafilatura(html, url):
    import trafilatura
    return trafilatura.extract(html, url=url, include_comments=False,
                               include_tables=True) or ""


def x_resiliparse(html, url):
    """C++/Cython, built for Common Crawl. Fast, higher recall, more boilerplate."""
    from resiliparse.extract.html2text import extract_plain_text
    return extract_plain_text(html, main_content=True, list_bullets=False,
                              alt_texts=False, links=False) or ""


def x_readability(html, url):
    from readability import Document
    from selectolax.lexbor import LexborHTMLParser
    return LexborHTMLParser(Document(html).summary()).text(separator=" ") or ""


def x_selectolax(html, url):
    """lexbor. Not a content extractor -- a very fast parser with the obvious
    furniture removed. This is the honest floor: what you get for ~0 cost."""
    from selectolax.lexbor import LexborHTMLParser
    tree = LexborHTMLParser(html)
    for tag in ("script", "style", "nav", "footer", "header", "aside", "form",
                "noscript", "svg"):
        for node in tree.css(tag):
            node.decompose()
    body = tree.body
    return body.text(separator="\n", strip=True) if body else ""


def x_soup(html, url):
    """What the cascade uses today as its last rung."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


EXTRACTORS = [
    ("trafilatura", x_trafilatura),
    ("resiliparse", x_resiliparse),
    ("readability", x_readability),
    ("selectolax", x_selectolax),
    ("soup(bs4)", x_soup),
]


def main():
    print("fetching pages once, so every extractor sees the same bytes...")
    pages = []
    for label, url in PAGES:
        try:
            payload, reason, final = f._tier_direct(url, 25)
            html = (payload or {}).get("html") or ""
            if len(html) > 2000:
                pages.append((label, final or url, html))
                print("  %-12s %6dk" % (label, len(html) // 1024))
            else:
                print("  %-12s skipped (%s)" % (label, reason or "too small"))
        except Exception as exc:                      # noqa: BLE001
            print("  %-12s skipped (%s)" % (label, type(exc).__name__))
    if not pages:
        raise SystemExit("no pages fetched")

    names = [n for n, _ in EXTRACTORS]
    chars = {n: [] for n in names}
    ms = {n: [] for n in names}
    junk = {n: [] for n in names}
    fails = dict.fromkeys(names, 0)

    print("\ncharacters recovered")
    print("%-12s %s" % ("page", "".join(n[:11].rjust(13) for n in names)))
    print("-" * (12 + 13 * len(names)))
    for label, url, html in pages:
        cells = []
        for name, fn in EXTRACTORS:
            started = time.perf_counter()
            try:
                text = fn(html, url) or ""
            except Exception:                         # noqa: BLE001
                fails[name] += 1
                text = ""
            elapsed = (time.perf_counter() - started) * 1000
            ms[name].append(elapsed)
            chars[name].append(len(text))
            junk[name].append(junk_ratio(text))
            cells.append(str(len(text)))
        print("%-12s %s" % (label, "".join(c.rjust(13) for c in cells)))

    print("-" * (12 + 13 * len(names)))
    print("%-12s %s" % ("median ms", "".join(
        ("%.1f" % statistics.median(ms[n])).rjust(13) for n in names)))
    print("%-12s %s" % ("junk %", "".join(
        ("%.1f%%" % (100 * statistics.mean(junk[n]))).rjust(13) for n in names)))
    print("%-12s %s" % ("failures", "".join(
        str(fails[n]).rjust(13) for n in names)))

    print("\nspeed relative to the fastest:")
    base = min(statistics.median(ms[n]) for n in names)
    for n in sorted(names, key=lambda k: statistics.median(ms[k])):
        print("  %-13s %5.1fx   (%.1f ms median)"
              % (n, statistics.median(ms[n]) / base, statistics.median(ms[n])))
    print("\nMore characters is NOT better -- check the junk column. An")
    print("extractor that returns the nav menu wins on size and loses on use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
