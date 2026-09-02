#!/usr/bin/env python3
"""Does keeping a session get us past walls that a cold request cannot?

Anti-bot systems hand out cookies -- Cloudflare's `__cf_bm` and `cf_clearance`
are the well-known pair -- that record "we have already looked at this client".
dethrottled currently makes every request from a standing start: bare `.get()`
calls, no cookie jar, nothing carried between them. So each fetch re-triggers
whatever evaluation the site does, and any clearance it grants is discarded
the moment the response is parsed.

That is a general defect, not a per-site trick, and this measures what fixing
it is worth. Three strategies against the same URLs:

  cold      one request, no state -- what the code does today
  session   a cookie jar reused across requests to that host
  warmed    visit the origin root first, then the target, on one session --
            which is what a person clicking a link actually does

Nothing here evades a rule. robots.txt is still the boundary; a cookie jar
just stops us re-introducing ourselves on every page.
"""
import sys
import time

TARGETS = [
    "https://www.loc.gov/collections/",
    "https://www.science.org/",
    "https://www.congress.gov/",
    "https://stackoverflow.com/questions/tagged/python",
    "https://www.g2.com/categories/crm",
]

PROFILE = "chrome"


def prose(html, url):
    from dethrottled import extract as fx
    if not html or len(html) < 800:
        return 0
    got = fx.extract(html, url=url, max_chars=200000)
    return got["chars"] if got["ok"] else 0


def cold(url):
    from curl_cffi import requests as creq
    r = creq.get(url, impersonate=PROFILE, timeout=25, allow_redirects=True)
    return r.status_code, prose(r.text, url), len(r.cookies or {})


def with_session(url):
    """One session, two requests to the same host: the second sees any cookie
    the first was given."""
    from curl_cffi import requests as creq
    with creq.Session(impersonate=PROFILE) as s:
        s.get(url, timeout=25, allow_redirects=True)
        time.sleep(1.2)
        r = s.get(url, timeout=25, allow_redirects=True)
        return r.status_code, prose(r.text, url), len(s.cookies or {})


def warmed(url):
    """Visit the origin root first, then the target -- what a person following
    a link does, and what a challenge cookie is issued during."""
    from curl_cffi import requests as creq
    root = "/".join(url.split("/")[:3]) + "/"
    with creq.Session(impersonate=PROFILE) as s:
        try:
            s.get(root, timeout=25, allow_redirects=True)
        except Exception:                             # noqa: BLE001
            pass
        time.sleep(1.5)
        r = s.get(url, timeout=25, allow_redirects=True,
                  headers={"Referer": root})
        return r.status_code, prose(r.text, url), len(s.cookies or {})


def run(label, fn, url):
    try:
        code, chars, cookies = fn(url)
        return "%s %5d %2dc" % (str(code)[:3], chars, cookies)
    except Exception as exc:                          # noqa: BLE001
        return type(exc).__name__[:12]


def main():
    print("status / characters of prose / cookies held\n")
    print("%-34s %16s %16s %16s" % ("url", "cold", "session", "warmed"))
    print("-" * 86)
    wins = {"cold": 0, "session": 0, "warmed": 0}
    for url in TARGETS:
        a = run("cold", cold, url)
        time.sleep(2)
        b = run("session", with_session, url)
        time.sleep(2)
        c = run("warmed", warmed, url)
        time.sleep(2)
        print("%-34s %16s %16s %16s" % (url.split("/")[2][:34], a, b, c))
        for name, res in (("cold", a), ("session", b), ("warmed", c)):
            parts = res.split()
            if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) >= 600:
                wins[name] += 1

    print("-" * 86)
    print("%-34s %16d %16d %16d" % ("SOLVED (>=600 chars)",
                                    wins["cold"], wins["session"], wins["warmed"]))
    print("\nIf session and warmed beat cold, the defect is that we discard")
    print("state -- not that the sites are unreachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
