#!/usr/bin/env python3
"""Polite HTTP fetching, with a ladder of free tiers.

No API keys are bought and no metered quota is consumed, but "free" does not
mean "one naive GET". Sites block datacentre IPs and render their content with
JavaScript, and a single tier loses to both:

    1. direct       requests + robots.txt        ~2.1s   most pages
    2. tls          a real browser TLS handshake  ~0.3s   beats fingerprinting
    3. crawl4ai     headless Chromium you host    ~4.4s   renders JavaScript

Only tier 1 is required. Tier 2 is a library, so it costs nothing to have.
Tier 3 needs a service you run and is skipped entirely when unconfigured. A
shorter ladder solves fewer pages and that is all -- nothing breaks.

There is deliberately NO relay tier. Every option needs either an account (a
Cloudflare Worker), or a third party who then learns every URL you fetch (a
hosted reader, a CORS proxy), or an address range that anti-bot vendors
blocklist on sight (Tor publishes its exit list in real time). None of those
belong in a tool whose whole claim is that it needs no account and no key.

**The important design point: a tier succeeds only if it yields readable
prose.** Escalating on failed fetches alone leaves the common case unsolved.
Measured: one publisher returns HTTP 200 with 52KB through a relay, another
returns 200 to a direct request, and neither response contains a word of
article text until the right tier runs. A ladder that stops at "HTTP 200"
declares victory on an empty shell. Thin results escalate too -- anything under
THIN_CHARS is treated as a miss.

robots.txt is honoured at every tier, and cached. A relay is a different route
to the same publisher, not permission to ignore what they asked for.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

PROJECT_URL = "https://github.com/zataraine/dethrottled"

# Honest and contactable, because that is the half of politeness robots.txt
# does not cover. Override it if you are running this as something else --
# but say what you are, and leave a way to be told to stop.
USER_AGENT = os.environ.get(
    "DETHROTTLED_USER_AGENT",
    "dethrottled/0.1 (+%s; automated fetcher; respects robots.txt)" % PROJECT_URL)
# Used ONLY for the two news RSS endpoints and the optional relay, both of
# which serve degraded or empty feeds to an obviously-automated agent. Article
# fetching uses USER_AGENT above, where robots.txt governs. Deliberately not
# arch-specific: the old string said aarch64 and told every server what it was
# talking to.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36")

# 10MB, not 3. A truncated HTML download is worse than a failed one: the parser
# still succeeds, on a document missing its tail, and reports a short article
# rather than an error. Real pages are already close to the old ceiling --
# tomshardware measured 2.34MB, 78% of it -- so this was going to start
# silently shortening articles rather than obviously breaking. Median is 0.36MB
# and the buffer is transient, so the headroom is close to free.
MAX_BYTES = int(os.environ.get("DETHROTTLED_MAX_HTML_BYTES", str(10_000_000)))

# Tracking params carry no meaning and split one article into several rows.
_JUNK_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
                "igshid", "s_cid", "cmpid", "spm", "_ga")


def canonical_url(url: str) -> str:
    """One URL per article, so the same page is not three rows.

    Strips tracking parameters, the fragment, a trailing slash, and the m. and
    amp. hosts that serve the same article at a different address. Deliberately
    string work: the pool showed 81 distinct domains across 109 rows, so
    duplication was never bad enough to justify hashing content, let alone
    embedding it.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return url or ""
    if not parts.netloc:
        return url or ""

    host = parts.netloc.lower()
    for prefix in ("www.", "m.", "amp."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not any(k.lower().startswith(j) for j in _JUNK_PARAMS)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme or "https", host, path,
                       urlencode(kept), ""))

DEFAULT_TIMEOUT = 20
MIN_DOMAIN_GAP = 1.5


# A real browser TLS fingerprint, without a browser.
#
# A great deal of what gets called "bot detection" never looks at your User-
# Agent: it looks at the shape of the TLS handshake -- cipher order, extension
# order, ALPN, the JA3/JA4 hash -- and every Python HTTP client has a
# conspicuously non-browser one. curl_cffi borrows a real Chrome's.
#
# Measured, this is not a slower fallback but a FASTER one: 310ms median
# against plain requests' 536ms on the same page set, because curl-impersonate
# is C. It sits second in the ladder for that reason.
#
# It does NOT render JavaScript, and it does not fix everything. Two hosts in
# testing returned 403 to every fingerprint tried -- chrome, chrome131,
# chrome124, safari17_0, safari15_5, edge101, firefox133 -- which is IP
# reputation, and no client-side trick touches that.
TLS_IMPERSONATE = os.environ.get("DETHROTTLED_TLS_IMPERSONATE", "chrome")

# Its own short timeout, and this is not a detail -- it is the whole reason the
# tier is safe to add.
#
# Measured: giving it the standard 20s turned a 25s bulk page budget into
# "direct, then tls, then nothing left". The renderer went from 6 solves to 4
# and outright failures went from 7 to 16, while tls itself solved 2. A tier
# that occasionally helps must never be able to spend the budget belonging to
# the tier that usually helps.
#
# 8s is generous for something whose median is 310ms. If it has not answered by
# then it was never going to be the cheap option.
TLS_TIMEOUT = int(os.environ.get("DETHROTTLED_TLS_TIMEOUT", "8"))
# Profile choice is not cosmetic. On one host chrome/safari/firefox all
# returned 200 and edge101 returned 403; on another the chrome family returned
# 163KB where every other profile returned 8KB.
ENABLE_TLS = os.environ.get("DETHROTTLED_ENABLE_TLS", "1").strip() not in (
    "0", "false", "no", "off", "")

# Empty by default. The render tier needs a Crawl4AI service you run; with no
# URL configured the tier is skipped entirely and the ladder is one rung
# shorter. `docker compose up` sets this for you.
CRAWL4AI_URL = os.environ.get("DETHROTTLED_CRAWL4AI_URL", "").rstrip("/")

# Which dialect the renderer speaks.
#
#   "auto"   probe once, remember the answer  (default)
#   "crawl"  upstream Crawl4AI: POST /crawl, port 11235
#   "render" a service exposing POST /render {url, timeout_ms}
#
# Two dialects because the upstream Docker image and a hand-rolled render
# worker are both perfectly reasonable things to point this at, and guessing
# wrong costs a silent dead tier rather than a loud error -- the failure this
# option exists to prevent. "auto" tries /crawl and falls back to /render, then
# caches which one answered so the cost is one extra request per process.
CRAWL4AI_API = os.environ.get("DETHROTTLED_CRAWL4AI_API", "auto").strip().lower()

# Remembered across calls within a process. None = not yet probed.
_crawl4ai_dialect = None if CRAWL4AI_API == "auto" else CRAWL4AI_API
# 25 rather than 90. Measured: this tier solved 1 page in 75 across two
# profiles. A ninety-second licence for a one-in-seventy-five tier means one
# page can cost more than the other forty-nine together.
CRAWL4AI_TIMEOUT = int(os.environ.get("DETHROTTLED_CRAWL4AI_TIMEOUT", "25"))

# The renderer's credential, when it has one.
#
# Upstream Crawl4AI refuses to listen on anything but loopback unless
# CRAWL4AI_API_TOKEN is set -- gunicorn's bind address is chosen from whether a
# credential exists. That is a sound default: a renderer reachable on a network
# with no auth is an open proxy that also runs a browser.
#
# So a containerised renderer necessarily has a token, and this must send it.
# Empty is normal for a loopback renderer that never needed one.
CRAWL4AI_TOKEN = os.environ.get("DETHROTTLED_CRAWL4AI_TOKEN", "").strip()


def _crawl4ai_headers() -> dict:
    return {"Authorization": "Bearer %s" % CRAWL4AI_TOKEN} if CRAWL4AI_TOKEN else {}

# A whole-page budget, checked before each tier. Twice the observed p99 of
# 12.3s and eight times the 2.9s median, so nothing that works today starts
# failing -- this exists only to stop one pathological page costing a minute.
# 60s, up from 25, because the ladder now has three tiers that can each take
# real time and 25 could not reach the third. direct can spend 20, crawl4ai 25,
# jina-reader 20: at the old budget a page that fell through to the renderer
# had nothing left for jina-reader, so reordering alone would have QUIETLY
# SHORTENED the ladder instead of lengthening it.
#
# This is the budget for a url somebody asked for by name. It is a ceiling, not
# a target: the median page still finishes on direct in about two seconds.
PAGE_BUDGET = float(os.environ.get("DETHROTTLED_PAGE_BUDGET", "60"))

# Bulk stays lean. search-and-extract runs its results one after another, so a
# generous per-page ceiling multiplies: six results at 60s each is six minutes
# for one search. Same one-versus-many split that governs OCR and the caps.
PAGE_BUDGET_BULK = float(os.environ.get("DETHROTTLED_PAGE_BUDGET_BULK", "25"))
# Must stay UNDER CRAWL4AI_TIMEOUT. It was 45000 against a 25s HTTP timeout,
# so we hung up at 25s on a renderer we had just asked to work for 45 -- paying
# the full wait, discarding the result, and leaving it rendering a page nobody
# would collect. 20s of render inside a 25s wait leaves slack for transport.
CRAWL4AI_RENDER_MS = int(os.environ.get("DETHROTTLED_CRAWL4AI_RENDER_MS", "20000"))
# Re-enabled 2026-08-26 after the worker was repaired.
#
# It was previously disabled because /render never returned: the service was
# capped at MemoryHigh=750M, and the kernel had throttled it into reclaim 7,392
# times. Two renders stalled, never released their semaphore slots, and every
# later request queued forever behind them while /health stayed green.
#
# Fixed at the source: limits raised to 1500M/2G, and the worker now bounds
# both slot acquisition and the render itself so a stall can no longer wedge
# it. Measured after the fix: example.com 2.1s, TrendForce 3.1s, DIGITIMES
# 3.4s, zero throttle events across 12 renders.
#
# It earns its slot in the ladder: DIGITIMES yields 432 chars through the CF
# Worker and 21,896 through the renderer.
# 40 an hour, up from 8. The 8 was sized for crawl4ai being the LAST resort,
# reached only by pages nothing else could solve. It is second now, so it is
# offered most pages that direct cannot parse, and 8 would have been spent by
# the second search of the hour -- turning the promotion into a demotion.
#
# The cost is local: about 4.7s of rendering each, so 40 is roughly three
# minutes of rendering per hour on your own hardware. jina-reader stays
# at 25 because it is external, it is now third so it is reached less, and
# being conservative with someone else's service is the right default.
CRAWL4AI_BUDGET = int(os.environ.get("DETHROTTLED_CRAWL4AI_BUDGET", "40"))

_locks = defaultdict(threading.Lock)
_last_hit = {}
_lock_guard = threading.Lock()
_tier_stats = defaultdict(int)

# Tier budgets refill on a rolling window rather than counting up forever.
#
# They used to be plain counters, reset only by reset_tier_stats(), which
# NOTHING CALLS. In a batch job that is a per-run cap, which is what it was
# written for. In a service that stays up for days it is a per-process-LIFETIME
# cap: after 8 renders crawl4ai stopped working entirely, and after 25 fetches
# so did jina-reader, until someone restarted the unit. The two most capable
# tiers in the ladder switched themselves off and then reported "budget_spent"
# as though that were a considered decision.
BUDGET_WINDOW = float(os.environ.get("DETHROTTLED_TIER_BUDGET_WINDOW", "3600"))
_spent = defaultdict(list)
_spend_guard = threading.Lock()


def _spend(tier: str, budget: int) -> bool:
    """Take one unit of a tier budget. False when the window is exhausted."""
    now = time.time()
    with _spend_guard:
        recent = [used for used in _spent[tier] if now - used < BUDGET_WINDOW]
        if len(recent) >= budget:
            _spent[tier] = recent
            return False
        recent.append(now)
        _spent[tier] = recent
        return True


def budget_state() -> dict:
    """How much of each tier's window is spent. For /stats, and for diagnosis."""
    now = time.time()
    with _spend_guard:
        return {tier: len([u for u in used if now - u < BUDGET_WINDOW])
                for tier, used in _spent.items()}


def tier_stats() -> dict:
    return dict(_tier_stats)


def reset_tier_stats() -> None:
    _tier_stats.clear()
    with _spend_guard:
        _spent.clear()


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _throttle(domain: str) -> None:
    with _lock_guard:
        lock = _locks[domain]
    with lock:
        wait = MIN_DOMAIN_GAP - (time.time() - _last_hit.get(domain, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[domain] = time.time()


def robots_allows(url: str, cache=None) -> bool:
    """Honoured at every tier. A relay is a route, not a permission slip."""
    domain = _domain(url)
    if not domain:
        return False
    parsed = urlparse(url)
    robots_url = "%s://%s/robots.txt" % (parsed.scheme or "https", parsed.netloc)

    rules = cache.get("robots", domain) if cache else None
    if rules is None:
        try:
            _throttle(domain)
            response = requests.get(robots_url, timeout=10,
                                    headers={"User-Agent": USER_AGENT})
            rules = response.text[:200_000] if response.status_code == 200 else ""
        except Exception:
            rules = ""
        if cache:
            cache.put("robots", domain, value=rules)
    if not rules:
        return True
    parser = RobotFileParser()
    parser.parse(rules.splitlines())
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True


# Below this a result is "thin": technically prose, but too little to be worth
# stopping on. Tracked separately from a plain success because a 432-character
# teaser off a paywall satisfies a naive length check while being useless: the
# fetch worked, the page is still not readable, and those are different facts.
THIN_CHARS = int(os.environ.get("DETHROTTLED_THIN_CHARS", "600"))


def _tidy_text(value: str, limit: int | None = None) -> str:
    """Collapse whitespace on text a tier already extracted for us."""
    import re as _re
    out = _re.sub(r"\s+", " ", str(value or "")).strip()
    if limit and len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0].strip()
    return out


# A truncated PDF is not a short PDF. The cross-reference table lives at the
# END of the file, so cutting the stream mid-download yields bytes no parser
# can open -- an 8.3MB statistics yearbook failed as "no text layer" for
# exactly that reason, against the 3MB MAX_BYTES meant for runaway HTML.
# So PDFs get their own, larger budget and are downloaded whole or not at all.
# Spreadsheets and decks are small next to PDFs, and anything claiming to be a
# 50MB xlsx is not a statistics table. Kept separate from PDF_MAX_BYTES so the
# two can move independently.
DOC_MAX_BYTES = int(os.environ.get("DETHROTTLED_DOC_MAX_BYTES", str(50 * 1024 * 1024)))

PDF_MAX_BYTES = int(os.environ.get("DETHROTTLED_PDF_MAX_BYTES", str(25 * 1024 * 1024)))


def _pdf_text(data: bytes, limit: int) -> str:
    """Text from a PDF, or "" if it has none.

    Rejecting PDFs outright at the content-type gate is the wrong call, because
    a great deal of primary material is published as a PDF and as nothing else
    -- statistical releases, standards, filings, tender documents. A source that
    only exists as a PDF would simply be unreachable.

    pymupdf was already installed. Pages are read until the limit is reached
    rather than all of them, because a 300-page annual report costs seconds to
    parse and the first few pages are what any cap keeps anyway.

    A scanned PDF has no text layer and returns "" here. That is correct: the
    caller then falls through to the next tier and reports it thin, rather than
    this claiming success with an empty body.
    """
    try:
        import pymupdf
    except ImportError:                               # pragma: no cover
        return ""
    parts, total = [], 0
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            chunk = page.get_text()
            parts.append(chunk)
            total += len(chunk)
            if total >= limit:
                break
    return "\n".join(parts)


def _usable(html: str) -> bool:
    return bool(html) and len(html) > 800


# ── tiers ────────────────────────────────────────────────────────────────────

def _tier_direct(url: str, timeout: int, allow_ocr: bool = False) -> tuple:
    domain = _domain(url)
    try:
        _throttle(domain)
        response = requests.get(
            url, timeout=timeout, stream=True, allow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
        if response.status_code != 200:
            return "", "http_%d" % response.status_code, url
        ctype = response.headers.get("Content-Type", "")
        if "pdf" in ctype.lower() or url.lower().split("?")[0].endswith(".pdf"):
            # Returned as text, not html, so it takes the already-extracted
            # path below and no local extractor re-parses it.
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > PDF_MAX_BYTES:
                return ("", "pdf_too_large:%dMB"
                        % (declared // (1024 * 1024)), str(response.url))
            data, size = [], 0
            for chunk in response.iter_content(65536):
                data.append(chunk)
                size += len(chunk)
                if size > PDF_MAX_BYTES:
                    # Whole or nothing: a partial buffer is not parseable.
                    return "", "pdf_too_large", str(response.url)
            blob = b"".join(data)
            text = _pdf_text(blob, CACHE_CHARS)
            if text.strip():
                return ({"text": text, "structured": True,
                         "content_type": "pdf"}, "", str(response.url))
            if not allow_ocr:
                return "", "pdf_no_text_layer", str(response.url)
            # Scanned. Tender documents, older yearbooks and municipal
            # filings arrive this way, and without OCR they are unreachable.
            # Measured at 1.4s a page, which is why this sits in the normal
            # path rather than behind a flag -- but only for single-URL
            # fetches, see allow_ocr.
            from .ocr import ocr_pdf
            text, why = ocr_pdf(blob, CACHE_CHARS)
            if not text.strip():
                return "", why or "pdf_no_text_layer", str(response.url)
            return ({"text": text, "structured": True, "content_type": "pdf"},
                    "", str(response.url))
        from . import documents as _docs
        suffix = os.path.splitext(url.lower().split("?")[0])[1]
        if (any(m in ctype.lower() for m in _docs.CONTENT_TYPES)
                or suffix in _docs.EXTENSIONS):
            data, size = [], 0
            for chunk in response.iter_content(65536):
                data.append(chunk)
                size += len(chunk)
                if size > DOC_MAX_BYTES:
                    return "", "document_too_large", str(response.url)
            blob = b"".join(data)
            kind = _docs.kind_of(blob, str(response.url), ctype)
            if kind:
                text, why = _docs.to_text(blob, kind, CACHE_CHARS)
                if text:
                    return ({"text": text, "structured": True,
                             "content_type": kind}, "", str(response.url))
                return "", why, str(response.url)
            # Claimed to be a document and is not one. The bytes are already
            # in hand, so treat them as the page they probably are rather than
            # spending another request finding out.
            html = blob.decode(response.encoding or "utf-8", errors="replace")
            if _usable(html):
                return {"html": html}, "", str(response.url)
            return "", "content_type:%s" % ctype[:30], url
        if "html" not in ctype and "xml" not in ctype:
            return "", "content_type:%s" % ctype[:30], url
        chunks, size, clipped = [], 0, False
        for chunk in response.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                clipped = True
                break
        html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        # Truncation used to be invisible. It has to be reported, because the
        # symptom is a short article rather than a failure, and a short article
        # is indistinguishable from a page that was simply short.
        return ({"html": html, "clipped": clipped},
                "html_truncated" if clipped else "", str(response.url))
    except Exception as exc:
        return {}, type(exc).__name__, url


# The one tier that leaves your network. It is free, keyless and
# unauthenticated -- but every URL you send it is a URL you have told somebody
# else you were interested in, and that is a real cost even when the money is
# zero.
#
# On by default in a plain pip install, because without a renderer it is the
# only tier that can read a JavaScript page. Off by default under `docker
# compose up`, where Crawl4AI does that job locally. Either way you decide:
#
#     DETHROTTLED_ENABLE_JINA=0    never contact it
#     dethrottled --no-jina        same, for one run
ENABLE_JINA = os.environ.get("DETHROTTLED_ENABLE_JINA", "1").strip() not in (
    "0", "false", "no", "off", "")
JINA_READER_URL = os.environ.get("DETHROTTLED_JINA_READER_URL", "https://r.jina.ai/")
# 20 rather than 35: no page in the profile needed more than 12.3s in total,
# across every tier it tried.
JINA_READER_TIMEOUT = int(os.environ.get("DETHROTTLED_JINA_READER_TIMEOUT", "20"))
# r.jina.ai is free and rate-limited by IP rather than metered, so there is no
# quota to exhaust -- but there IS an IP to get blocked. Bounded per run.
JINA_READER_BUDGET = int(os.environ.get("DETHROTTLED_JINA_READER_BUDGET", "25"))


def _tier_jina_reader(url: str) -> tuple:
    """Unauthenticated r.jina.ai. Free, and it walks through bot walls.

    Unauthenticated by design: r.jina.ai is free and rate-limited rather than
    metered, so sending it a key buys nothing. Measured returning 69,107
    characters from a page direct fetching could not touch at all.

    It is an external service, which is the one real cost: every URL passed
    through it is a URL you have told somebody else about. That is why it sits
    behind the local renderer rather than in front of it.

    It returns markdown, already extracted -- so it is handed back as TEXT, not
    HTML, and skips the local extractor entirely.
    """
    if not ENABLE_JINA:
        return {}, "jina_reader_disabled", url
    if not _spend("jina-reader", JINA_READER_BUDGET):
        return {}, "jina_budget_spent", url
    try:
        response = requests.get(
            JINA_READER_URL + url,
            timeout=JINA_READER_TIMEOUT,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "text/markdown, text/plain;q=0.9",
                     "X-Respond-With": "markdown"},
        )
        if response.status_code != 200:
            return {}, "jina_http_%d" % response.status_code, url
        text = response.text or ""
        if len(text) > MAX_BYTES:
            text = text[:MAX_BYTES]
        return {"text": text}, "", url
    except Exception as exc:
        return {}, "jina_" + type(exc).__name__, url


def _crawl4ai_payload(payload: dict, url: str) -> tuple:
    """Pull usable content out of whatever shape the renderer returned.

    Crawl4AI ALREADY EXTRACTS, and its extraction is better than re-extracting
    from its HTML. Measured on a JavaScript-heavy article: the cleaned HTML was
    6,586 characters that trafilatura could not read at all, while the markdown
    held 2,339 characters of clean prose. Running a local extractor over
    somebody else's good extraction threw the good extraction away.

    So markdown wins when there is enough of it, and the HTML fields are the
    fallback for a renderer that does not produce markdown.
    """
    final = payload.get("url") or url

    markdown = payload.get("markdown")
    # Upstream returns either a string or {raw_markdown, fit_markdown, ...}.
    # fit_markdown is the filtered one and is preferred when present, because
    # unfiltered markdown of a news page is mostly navigation.
    if isinstance(markdown, dict):
        markdown = (markdown.get("fit_markdown")
                    or markdown.get("raw_markdown")
                    or markdown.get("markdown"))
    if isinstance(markdown, str) and len(markdown.strip()) > 220:
        return {"text": markdown}, "", final

    for field in ("cleaned_html", "fit_html", "content", "html", "raw_html"):
        value = payload.get(field)
        if isinstance(value, str) and _usable(value):
            return {"html": value}, "", final
    return {}, "crawl4ai_empty", url


def _crawl4ai_via_crawl(url: str) -> tuple:
    """Upstream Crawl4AI's Docker API: POST /crawl on :11235.

    The response wraps results in a list, because the endpoint takes a list of
    URLs. One URL in, one result out, but the envelope is still there.
    """
    response = requests.post(
        CRAWL4AI_URL + "/crawl",
        json={"urls": [url],
              "browser_config": {"type": "BrowserConfig",
                                 "params": {"headless": True}},
              "crawler_config": {"type": "CrawlerRunConfig",
                                 "params": {"cache_mode": "bypass",
                                            "page_timeout": CRAWL4AI_RENDER_MS}}},
        headers=_crawl4ai_headers(),
        timeout=CRAWL4AI_TIMEOUT)
    if response.status_code != 200:
        return {}, "crawl4ai_http_%d" % response.status_code, url
    body = response.json()
    if not isinstance(body, dict):
        return {}, "crawl4ai_shape", url
    # `success: false` is a rendered failure, not a transport failure, and it
    # carries the reason. Reporting it beats reporting "empty".
    results = body.get("results")
    payload = (results[0] if isinstance(results, list) and results else body)
    if not isinstance(payload, dict):
        return {}, "crawl4ai_shape", url
    if payload.get("success") is False:
        reason = str(payload.get("error_message") or "render_failed")[:40]
        return {}, "crawl4ai_" + reason, url
    return _crawl4ai_payload(payload, url)


def _crawl4ai_via_render(url: str) -> tuple:
    """A service exposing POST /render {url, timeout_ms}."""
    response = requests.post(
        CRAWL4AI_URL + "/render",
        json={"url": url, "timeout_ms": CRAWL4AI_RENDER_MS},
        headers=_crawl4ai_headers(),
        timeout=CRAWL4AI_TIMEOUT)
    if response.status_code != 200:
        return {}, "crawl4ai_http_%d" % response.status_code, url
    try:
        payload = response.json()
    except ValueError:
        return {"html": response.text}, "", url
    if not isinstance(payload, dict):
        return {}, "crawl4ai_shape", url
    return _crawl4ai_payload(payload, url)


# ── tier cooldown ────────────────────────────────────────────────────────────
#
# Tiers get rate limited, and a rate limit is a temporary condition wearing the
# costume of a permanent one. Asking a tier that just said 429 to try the very
# next page wastes a timeout and deepens the limit; dropping it permanently
# throws away a tier that will be fine in ten minutes.
#
# So a tier that refuses is RESTED: skipped for a cooling period, then tried
# again. One probe every cooling period costs almost nothing and the tier heals
# itself without anyone restarting anything.
#
# Backoff doubles per consecutive refusal, capped, and resets the moment the
# tier answers -- so a brief wobble costs one cooldown while a genuinely dead
# tier settles at the cap instead of being asked constantly.
TIER_COOLDOWN = float(os.environ.get("DETHROTTLED_TIER_COOLDOWN", "300"))
TIER_COOLDOWN_MAX = float(os.environ.get("DETHROTTLED_TIER_COOLDOWN_MAX", "3600"))

# What counts as "you are asking too often", as opposed to "that page is not
# here". A 404 says nothing about the tier; a 429 says everything.
_RATE_LIMIT_CODES = (401, 403, 407, 418, 429, 503)

# ONLY these can be rested, and getting this wrong is expensive.
#
# A cooldown is correct for a tier that is a single shared resource: one
# renderer, one relay, one hosted reader. Refuse once and the next request will
# be refused too, so standing back is right.
#
# It is flatly WRONG for `direct` and `tls`, which are not services at all --
# they are "make an HTTP request", per host. Measured, when they were included:
# one page 403'd, the direct tier was rested globally, and every subsequent
# page in the run skipped straight past it to the renderer. direct went from 42
# solves to 7, the renderer was asked to do 30 pages it had no business doing,
# and extraction fell from 87% to 67%.
#
# A 403 from one host says nothing whatsoever about the next host. Per-host
# politeness is already handled, by robots.txt and the per-domain request gap.
_RESTABLE_TIERS = frozenset(
    e.strip() for e in os.environ.get(
        "DETHROTTLED_RESTABLE_TIERS", "crawl4ai,jina-reader").split(",")
    if e.strip())

_tier_rest = {}                      # tier -> {"until": ts, "strikes": n}
_tier_rest_lock = threading.Lock()


def tier_resting(name: str) -> bool:
    """Is this tier in its cooling period?"""
    if name not in _RESTABLE_TIERS:
        return False
    with _tier_rest_lock:
        entry = _tier_rest.get(name)
        return bool(entry and time.time() < entry["until"])


def tier_refused(name: str, reason: str = "") -> None:
    """Record a refusal and start (or extend) the cooling period.

    A no-op for per-host tiers: see _RESTABLE_TIERS for why that distinction
    is the whole safety of this mechanism.
    """
    if name not in _RESTABLE_TIERS:
        return
    with _tier_rest_lock:
        entry = _tier_rest.get(name) or {"strikes": 0}
        entry["strikes"] += 1
        # The shift is capped BEFORE the power is computed, not after.
        # min(TIER_COOLDOWN * 2 ** (strikes - 1), MAX) reads as if it is
        # bounded, and is not: Python integers do not overflow, so a tier
        # refused a few thousand times built a number with hundreds of digits
        # and raised OverflowError converting it to a float -- only ever under
        # sustained failure, which is exactly when the cooldown matters.
        # Measured: 256 exceptions in a 1,280-refusal stress run.
        shift = min(max(entry["strikes"] - 1, 0), 20)
        entry["until"] = time.time() + min(TIER_COOLDOWN * (2 ** shift),
                                           TIER_COOLDOWN_MAX)
        entry["reason"] = reason[:60]
        _tier_rest[name] = entry


def tier_answered(name: str) -> None:
    """Clear the record. A tier that works is not on a warning."""
    with _tier_rest_lock:
        _tier_rest.pop(name, None)


def tier_rest_state() -> dict:
    """What is resting and for how long. Surfaced by /stats, because a silently
    skipped tier is exactly the kind of degradation that hides for days."""
    now = time.time()
    with _tier_rest_lock:
        return {name: {"resting_for": max(0, int(e["until"] - now)),
                       "strikes": e["strikes"], "reason": e.get("reason", "")}
                for name, e in _tier_rest.items() if now < e["until"]}


def _is_refusal(reason: str) -> bool:
    """Does this failure mean 'stop asking' rather than 'that page is missing'?"""
    if not reason:
        return False
    if "http_" in reason:
        for code in _RATE_LIMIT_CODES:
            if reason.endswith("_%d" % code):
                return True
        return False
    # Transport-level give-ups: the host hung up, or never answered.
    return any(word in reason for word in
               ("Timeout", "ConnectionError", "TooManyRedirects", "SSLError"))


def _tier_tls(url: str) -> tuple:
    """Refetch with a browser's TLS fingerprint. Cheap, fast, no browser.

    Optional: without curl_cffi installed this reports itself unavailable and
    the ladder is one rung shorter, which is the same contract every other
    optional tier honours.
    """
    if not ENABLE_TLS:
        return {}, "tls_disabled", url
    try:
        from curl_cffi import requests as tls_requests
    except ImportError:
        return {}, "tls_not_installed", url
    from ._quiet import quiet_fd2
    try:
        # See _quiet.quiet_fd2: the HTTP/2 layer prints an unclean-shutdown
        # complaint from Rust on some hosts, after the response has already
        # arrived intact. Nothing actionable, and noisy on every such fetch.
        with quiet_fd2():
            response = tls_requests.get(
                url, impersonate=TLS_IMPERSONATE, timeout=TLS_TIMEOUT,
                allow_redirects=True)
    except Exception as exc:
        return {}, "tls_" + type(exc).__name__, url
    if response.status_code >= 400:
        return {}, "tls_http_%d" % response.status_code, str(response.url)
    # Same size ceiling as the direct tier: a browser fingerprint is not a
    # reason to accept an unbounded download.
    text = response.text or ""
    if len(text) > MAX_BYTES:
        text = text[:MAX_BYTES]
    return {"html": text}, "", str(response.url)


def _tier_crawl4ai(url: str) -> tuple:
    """Headless Chromium you host. Renders JavaScript. Slow, strictly budgeted.

    Optional. With DETHROTTLED_CRAWL4AI_URL unset this never runs and the ladder
    is one tier shorter, which is a smaller stack rather than a broken one.

    This is the tier that makes the external reader service optional: a page
    whose content does not exist until JavaScript has run is unreadable to a
    plain fetcher, and a renderer you run yourself is the only way to read it
    without telling somebody else which URL you wanted.
    """
    global _crawl4ai_dialect
    if not CRAWL4AI_URL:
        return {}, "crawl4ai_not_configured", url
    if not _spend("crawl4ai", CRAWL4AI_BUDGET):
        return {}, "crawl4ai_budget_spent", url

    order = ([_crawl4ai_dialect] if _crawl4ai_dialect
             else ["crawl", "render"])
    last = "crawl4ai_empty"
    for dialect in order:
        run = _crawl4ai_via_crawl if dialect == "crawl" else _crawl4ai_via_render
        try:
            payload, reason, final = run(url)
        except Exception as exc:
            last = "crawl4ai_" + type(exc).__name__
            continue
        # A 404 means this endpoint is not the one this server speaks. Any
        # other answer -- including an honest render failure -- means we found
        # the right dialect and should stop probing for it.
        if reason == "crawl4ai_http_404":
            last = reason
            continue
        _crawl4ai_dialect = dialect
        return payload, reason, final
    return {}, last, url


# What gets EXTRACTED and CACHED, independent of what the caller asked for.
#
# The cache used to be keyed on (url, max_chars), which meant raising the
# /extract cap from 3500 to 8000 orphaned all 904 cached extractions at a
# stroke -- every page became a cold fetch, on a tier ladder where the hard
# targets are exactly the ones that fail. That is a bad property: it makes
# tuning a cap quietly expensive, so caps do not get tuned.
#
# So extract once, generously, and clip on the way out. One entry per page, and
# a cap change costs nothing. 20k is well past any cap worth setting; pages
# longer than this are truncated in the cache too, which is the same trade the
# old code made at 3500.
CACHE_CHARS = int(os.environ.get("DETHROTTLED_EXTRACT_CACHE_CHARS", "20000"))


def _clip(out: dict, max_chars: int) -> dict:
    """A copy of a cached extraction, cut to what this caller asked for."""
    text = out.get("text") or ""
    if len(text) <= max_chars:
        return out
    clipped = dict(out)
    clipped["text"] = text[:max_chars]
    clipped["chars"] = len(clipped["text"])
    return clipped


def fetch_and_extract(url: str, *, max_chars: int = 3500, timeout: int = DEFAULT_TIMEOUT,
                      cache=None, obey_robots: bool = True,
                      allow_render: bool = True,
                      allow_ocr: bool = True,
                      page_budget: float | None = None,
                      keep_html: bool = False) -> dict:
    """Run the ladder with EXTRACTION as the success test, not fetch.

    This is the important correction. Escalating only when a fetch fails leaves
    the common case unsolved: TrendForce returns 200 with 52KB through the CF
    Worker and DIGITIMES returns 200 directly, yet neither yields a word of
    article text, because the body arrives via JavaScript. A ladder that stops
    at "HTTP 200" declares victory on an empty shell.

    So each tier is judged on whether it produced readable prose. A cheap tier
    that returns a JS skeleton is a miss, and the page escalates to the
    renderer that can actually resolve it.

    Returns {ok, text, tier, extractor, title, published, chars, url, reason}.
    """
    from . import extract as _fx

    # keep_html deliberately bypasses the cache. The cache stores the
    # extracted text and NOT the source -- storing megabytes of HTML per page
    # would trade many-pages-cheaply for few-pages-expensively -- so a caller
    # who asked for the source cannot be served from it, and quietly returning
    # a row with no html would be the wrong kind of surprise.
    if cache is not None and not keep_html:
        # Keyed on the url alone. The stored copy is the generous one; what
        # this caller wanted is a clip of it.
        hit = cache.get("extract", url)
        if hit is not None:
            hit["cached"] = True
            _tier_stats["cache"] += 1
            return _clip(hit, max_chars)

    if obey_robots and not robots_allows(url, cache=cache):
        _tier_stats["robots_blocked"] += 1
        return {"ok": False, "text": "", "tier": None, "extractor": None,
                "title": "", "published": "", "chars": 0, "url": url,
                "reason": "robots_disallow", "cached": False}

    # Order set by measurement, not intuition -- see tests/test_tier_matrix.py,
    # which forces each tier independently over a fixed URL set and prints the
    # characters of usable prose each one recovers.
    # Across 8 pages, forcing each tier independently:
    #   direct       4 solved,  2.1s avg
    #   jina-reader  6 solved,  4.8s avg, richest text by a wide margin
    #   crawl4ai     7 solved,  4.4s avg, the ONLY tier to solve 1 page alone
    # crawl4ai sits AHEAD of jina-reader, reversing the earlier order. Two
    # reasons, both from the same eight-page matrix:
    #
    #   direct       3 solved, 1978ms
    #   crawl4ai     7 solved, 4723ms, the ONLY tier to solve one page alone
    #   jina-reader  6 solved, 4043ms
    #
    # crawl4ai solves MORE, and it runs on this box while jina-reader is an
    # external service that learns every url we look at. Trying the local
    # renderer first costs a few hundred ms on pages jina would also have
    # solved, and buys both the extra page and a smaller external footprint.
    # jina-reader is kept rather than dropped: it still rescues pages crawl4ai
    # cannot render, and its text is rich when it works.
    tiers = [("direct", lambda: _tier_direct(url, timeout, allow_ocr))]
    # Second, because it is the cheapest thing that can succeed where `direct`
    # failed -- faster than direct in measurement, and it needs no service.
    if ENABLE_TLS:
        tiers.append(("tls", lambda: _tier_tls(url)))
    if allow_render and CRAWL4AI_URL and CRAWL4AI_BUDGET > 0:
        tiers.append(("crawl4ai", lambda: _tier_crawl4ai(url)))
    if ENABLE_JINA:
        tiers.append(("jina-reader", lambda: _tier_jina_reader(url)))

    reasons, best = [], None
    budget = PAGE_BUDGET if page_budget is None else page_budget
    budget_started = time.time()

    def finish(out):
        # Store the full extraction, hand back only what was asked for.
        #
        # The source, when a caller asked for it, is deliberately NOT cached.
        # It is often twenty times the size of the text extracted from it, and
        # caching it would trade the whole point of a page cache -- many pages
        # cheaply -- for a few pages expensively.
        source = out.pop("html", None)
        if cache is not None:
            cache.put("extract", url, value=out)
        out = _clip(out, max_chars)
        if source is not None:
            out["html"] = source
        return out

    # ── video, decided from the URL before anything is fetched ──────────
    #
    # A video page is the one case the ladder cannot win. Fetching it returns a
    # player, rendering it returns a player, and no extractor recovers prose
    # from either -- the words live in a caption track served separately.
    #
    # So it is routed here, on the URL alone, at the cost of one regex against
    # a host allowlist. Anything that is not a video URL falls straight through
    # to the ladder having spent no network at all.
    from . import media as _media
    if _media.is_video(url):
        text, _title, why = _media.transcript(url, limit=CACHE_CHARS)
        if text:
            out = {"ok": True, "text": text, "tier": "transcript",
                   "extractor": "captions", "title": "", "published": "",
                   "chars": len(text), "url": url, "reason": "",
                   "cached": False, "content_type": "transcript"}
            return finish(out)
        # No captions, or blocked. Reported under its own name rather than
        # falling through to a ladder that would spend four tiers proving it
        # cannot read a video player.
        _tier_stats["failed"] += 1
        return {"ok": False, "text": "", "tier": None, "extractor": None,
                "title": "", "published": "", "chars": 0, "url": url,
                "reason": why or "no_transcript", "cached": False}

    for name, run in tiers:
        # A resting tier is skipped without being asked. This is the smooth
        # transition: the ladder simply gets shorter for a while and the rungs
        # that still work carry the load.
        if tier_resting(name):
            reasons.append("%s:resting" % name)
            continue
        # Checked before each tier rather than enforced during one: a tier
        # already running is allowed to finish, because killing a fetch that is
        # about to succeed wastes everything spent on it. This stops the NEXT
        # attempt, which is where a runaway page actually costs its minute.
        if time.time() - budget_started > budget:
            if best is None:
                reasons.append("%s: skipped, page budget of %.0fs spent"
                               % (name, budget))
            break
        payload, reason, final = run()
        payload = payload if isinstance(payload, dict) else {}
        if _is_refusal(reason):
            tier_refused(name, reason)
        elif payload:
            # Answered with something. Whether it extracted is a separate
            # question -- an empty shell is the page's fault, not the tier's,
            # and resting a tier for it would be wrong.
            tier_answered(name)
        candidate = None

        # A tier may hand back already-extracted text. Re-running a local
        # extractor over someone else's good extraction only loses content.
        supplied = payload.get("text")
        # The 220 floor exists to reject junk from HTML extractors. A document
        # that PARSED is a different case: a five-row statistics table or a
        # one-page tender notice is short and completely real, and rejecting it
        # sent both to the html branch to be reported as "empty".
        structured_floor = 1 if payload.get("structured") else 220
        if isinstance(supplied, str) and len(_tidy_text(supplied)) >= structured_floor:
            # A table's row boundaries carry meaning: run two rows together
            # and it no longer says which number belongs to which year. So
            # spreadsheets, OCR pages and slides keep their line breaks.
            if payload.get("structured"):
                from . import documents as _docs
                text = _docs.tidy_structured(supplied, limit=CACHE_CHARS)
            else:
                text = _tidy_text(supplied, limit=CACHE_CHARS)
            source = {"html": payload.get("html") or ""} if keep_html else {}
            candidate = {**source,
                         "ok": True, "text": text, "tier": name,
                         "extractor": "%s-native" % name, "title": "",
                         "published": "", "chars": len(text), "url": final,
                         "reason": "", "cached": False,
                         "content_type": payload.get("content_type", "")}
        else:
            html = payload.get("html") or ""
            if not _usable(html):
                reasons.append("%s:%s" % (name, reason or "empty"))
                continue
            result = _fx.extract(html, url=final, max_chars=CACHE_CHARS)
            if result["ok"]:
                # The source, only when asked for. finish() strips it before
                # the cache write, so this never bloats the page cache.
                source = {"html": html} if keep_html else {}
                candidate = {**source,
                             "ok": True, "text": result["text"], "tier": name,
                             "extractor": result["tier"],
                             "title": result.get("title", ""),
                             "published": result.get("published", ""),
                             "chars": result["chars"], "url": final,
                             "reason": ("html_truncated"
                                        if payload.get("clipped") else ""),
                             "cached": False, "content_type": "html"}
            else:
                # Fetched fine, but no prose -- almost always a JS shell.
                reasons.append("%s:fetched_but_%s" % (name, result["reason"]))
                continue

        # Structured payloads are never "thin". THIN_CHARS asks whether a page
        # gave up only a teaser, which is a question about HTML -- no later
        # tier is going to read a spreadsheet or a scan better than the parser
        # that already read it, so trying them only burns the page budget.
        if candidate["chars"] >= THIN_CHARS or payload.get("structured"):
            _tier_stats[name] += 1
            return finish(candidate)

        # Thin: keep it as a floor and let a later tier try to beat it, rather
        # than either discarding a real result or stopping on a teaser.
        reasons.append("%s:thin/%d" % (name, candidate["chars"]))
        if best is None or candidate["chars"] > best["chars"]:
            best = candidate

    if best is not None:
        _tier_stats[best["tier"] + "-thin"] += 1
        best["reason"] = "thin, best available: " + ", ".join(reasons)
        return finish(best)

    _tier_stats["failed"] += 1
    return {"ok": False, "text": "", "tier": best, "extractor": None,
            "title": "", "published": "", "chars": 0, "url": url,
            "reason": " | ".join(reasons), "cached": False}
