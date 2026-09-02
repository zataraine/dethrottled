#!/usr/bin/env python3
"""Which TLS fingerprint gets through, and can any extractor read the result?

Two questions the first probe raised and could not answer:

1. `direct` and a default-impersonation curl_cffi both eat a 403 from some
   hosts. Is that TLS fingerprinting we can defeat with a better profile, or
   is it IP reputation, which no client-side trick will fix?

2. Reddit returned 163KB through curl_cffi and 0 characters of prose. A fetch
   that works followed by an extractor that does not is a completely different
   bug from a fetch that fails, and it is invisible if you only measure the end.
"""
import sys
import time

from curl_cffi import requests as creq

from dethrottled import extract as fx

# The ones nothing solved, plus one control that everything solves.
TARGETS = [
    ("stackoverflow", "https://stackoverflow.com/questions/tagged/python"),
    ("g2",            "https://www.g2.com/categories/crm"),
    ("indeed",        "https://www.indeed.com/"),
    ("reddit",        "https://www.reddit.com/r/programming/"),
    ("wikipedia",     "https://en.wikipedia.org/wiki/Web_scraping"),
]

# curl_cffi ships fingerprints for real browser builds. Newer is not always
# better: a site tuned against last year's Chrome may pass an older profile and
# block the current one, so this sweeps rather than guesses.
PROFILES = ["chrome", "chrome131", "chrome124", "safari17_0", "safari15_5",
            "edge101", "firefox133"]


def fetch(url, profile):
    started = time.time()
    try:
        r = creq.get(url, impersonate=profile, timeout=25, allow_redirects=True)
        return r.status_code, r.text, int((time.time() - started) * 1000)
    except Exception as exc:
        return type(exc).__name__, "", int((time.time() - started) * 1000)


def main():
    print("=" * 74)
    print("PART 1 — which TLS fingerprint gets a 200?")
    print("=" * 74)
    print("%-16s %s" % ("page", "".join(p[:9].rjust(11) for p in PROFILES)))
    print("-" * (16 + 11 * len(PROFILES)))

    best_html = {}
    for label, url in TARGETS:
        cells = []
        for profile in PROFILES:
            status, html, _ms = fetch(url, profile)
            if status == 200:
                cells.append("200/%dk" % (len(html) // 1024))
                # keep the biggest 200 for part 2
                if len(html) > len(best_html.get(label, ("", ""))[1] or ""):
                    best_html[label] = (url, html)
            else:
                cells.append(str(status)[:9])
            time.sleep(0.4)          # one host, several requests: be polite
        print("%-16s %s" % (label, "".join(c.rjust(11) for c in cells)))

    print()
    print("=" * 74)
    print("PART 2 — given HTML that DID arrive, which extractor reads it?")
    print("=" * 74)

    # Every extractor in the cascade, run individually rather than as a
    # fallback chain, so a tier-one success cannot hide a tier-three failure.
    def with_trafilatura(html, url):
        import trafilatura
        return trafilatura.extract(html, url=url, include_comments=False) or ""

    def with_readability(html, url):
        from bs4 import BeautifulSoup
        from readability import Document
        summary = Document(html).summary()
        return BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)

    def with_soup(html, url):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        return soup.get_text(" ", strip=True)

    def with_cascade(html, url):
        return fx.extract(html, url=url, max_chars=500000).get("text", "")

    extractors = [("trafilatura", with_trafilatura),
                  ("readability", with_readability),
                  ("soup", with_soup),
                  ("cascade", with_cascade)]

    print("%-16s %8s %s" % ("page", "html", "".join(n.rjust(13) for n, _ in extractors)))
    print("-" * (25 + 13 * len(extractors)))
    for label, url in TARGETS:
        if label not in best_html:
            print("%-16s %8s  (nothing fetched)" % (label, "-"))
            continue
        url, html = best_html[label]
        cells = []
        for _name, fn in extractors:
            try:
                cells.append(str(len(fn(html, url) or "")))
            except Exception as exc:
                cells.append(type(exc).__name__[:11])
        print("%-16s %7dk %s" % (label, len(html) // 1024,
                                 "".join(c.rjust(13) for c in cells)))

    print("\nA big number under `soup` with a 0 under `trafilatura` means the")
    print("page WAS retrieved and the extractor threw it away -- a different")
    print("problem, and a fixable one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
