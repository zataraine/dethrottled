#!/usr/bin/env python3
"""Four sites that refuse us. What actually gets in?

Not a bypass study. dethrottled honours robots.txt at every tier and that is
not up for negotiation -- the question here is narrower and more interesting:
are we being *misclassified*, and does the site publish a machine-readable
endpoint it would rather we used?

Four hypotheses, cheapest first:

  1. our request is under-dressed -- a real browser sends a dozen headers and
     we send three, which is a fingerprint all by itself
  2. HTTP/1.1 versus HTTP/2 -- browsers speak h2 to these hosts
  3. the site has an official keyless API, and the HTML page was never the
     right thing to fetch
  4. it is the address, not the request, in which case nothing above helps

    python scripts/probe_blocked.py
"""
import json
import sys
import time
import urllib.request

TARGETS = [
    ("loc.gov", "https://www.loc.gov/collections/"),
    ("science.org", "https://www.science.org/"),
    ("congress.gov", "https://www.congress.gov/"),
    ("stackoverflow", "https://stackoverflow.com/questions/tagged/python"),
]

# What Chrome actually sends. Our direct tier sends a User-Agent and little
# else, which is itself a tell: no real browser omits Accept-Language or the
# Sec-Fetch metadata.
BROWSERISH = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/140.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-CH-UA": '"Chromium";v="140", "Not=A?Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
}


def status(fn):
    try:
        return fn()
    except Exception as exc:
        return "%s" % type(exc).__name__


def plain_requests(url, headers=None):
    import requests
    r = requests.get(url, headers=headers or {}, timeout=25,
                     allow_redirects=True)
    return "%d/%dk" % (r.status_code, len(r.text) // 1024)


def tls_impersonate(url, profile="chrome", headers=None):
    """curl_cffi sets a browser TLS AND HTTP/2 fingerprint together."""
    from curl_cffi import requests as creq
    r = creq.get(url, impersonate=profile, timeout=25, headers=headers or {},
                 allow_redirects=True)
    return "%d/%dk" % (r.status_code, len(r.text) // 1024)


def main():
    from dethrottled import fetch as f

    print("=" * 74)
    print("1 & 2. IS IT THE REQUEST? headers and fingerprint")
    print("=" * 74)
    print("%-15s %12s %12s %12s %12s" % (
        "site", "our UA", "browser hdrs", "tls chrome", "tls+headers"))
    print("-" * 74)
    for name, url in TARGETS:
        a = status(lambda u=url: plain_requests(u, {"User-Agent": f.USER_AGENT}))
        b = status(lambda u=url: plain_requests(u, BROWSERISH))
        c = status(lambda u=url: tls_impersonate(u))
        d = status(lambda u=url: tls_impersonate(u, headers=BROWSERISH))
        print("%-15s %12s %12s %12s %12s" % (name, a, b, c, d))
        time.sleep(1.5)

    print()
    print("=" * 74)
    print("3. IS THERE AN OFFICIAL KEYLESS ENDPOINT?")
    print("=" * 74)

    # The genuinely cheeky answer: three of these four publish machine-readable
    # data that is not behind the wall, because they WANT it fetched that way.
    endpoints = [
        ("loc.gov JSON",
         "https://www.loc.gov/collections/?fo=json",
         "official JSON API, no key"),
        ("loc.gov search",
         "https://www.loc.gov/search/?q=cartography&fo=json",
         "same API, any page + fo=json"),
        ("stackexchange API",
         "https://api.stackexchange.com/2.3/questions?site=stackoverflow"
         "&tagged=python&pagesize=3&order=desc&sort=votes",
         "keyless up to 300/day"),
        ("govinfo bulk",
         "https://www.govinfo.gov/bulkdata/json/BILLSTATUS/119",
         "congressional data, keyless"),
        ("crossref (science.org DOIs)",
         "https://api.crossref.org/works/10.1126/science.adi2336",
         "metadata for any DOI, keyless"),
    ]
    for label, url, note in endpoints:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": f.USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read(400000)
            try:
                parsed = json.loads(body)
                shape = (list(parsed)[:4] if isinstance(parsed, dict)
                         else "list[%d]" % len(parsed))
            except ValueError:
                shape = "not json"
            print("  %-28s %6d  %-24s %s" % (label, len(body), str(shape)[:24], note))
        except Exception as exc:
            print("  %-28s FAILED  %s" % (label, str(exc)[:40]))
        time.sleep(1.0)

    print()
    print("=" * 74)
    print("4. IS IT THE ADDRESS? (robots.txt is served to everyone)")
    print("=" * 74)
    # If robots.txt comes back but the page does not, we are being classified
    # rather than firewalled -- the connection is fine.
    for name, url in TARGETS:
        root = "/".join(url.split("/")[:3])
        got = status(lambda u=root: plain_requests(u + "/robots.txt", BROWSERISH))
        print("  %-15s robots.txt: %s" % (name, got))
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
