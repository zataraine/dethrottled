#!/usr/bin/env python3
"""Free article extraction: trafilatura, then resiliparse, then selectolax.

All local, all open source, no service call, and every rung ships an aarch64
wheel -- this runs on a Raspberry Pi without a compiler.

The order was set by measuring all five candidates on the same ten pages,
scoring both what they recovered and how much of it was page furniture:

    extractor      median ms   junk %   note
    resiliparse          2.0     5.5%   fastest; misses main content on some
                                        layouts entirely (78 chars on a page
                                        trafilatura read 7,445 from)
    selectolax           2.6     3.1%   fast, and never returns nothing
    readability         27.4     1.5%   low junk because it often returns
                                        almost nothing: 0 chars on one page,
                                        239 on another
    trafilatura         33.4     0.9%   cleanest output, best coverage
    BeautifulSoup       37.2    66.5%   slowest AND two thirds boilerplate

So trafilatura leads on quality. resiliparse is second because it is sixteen
times faster and catches real trafilatura misses -- on one news front page it
recovered 13,955 characters where trafilatura found 3,545. selectolax is the
floor: not a content extractor at all, just a very fast parser with the
furniture removed, which is exactly what a last resort should be.

readability and BeautifulSoup were both dropped. BeautifulSoup was the worst
rung on both axes at once, and readability rarely earned its 27ms.

The cascade matters more than the leader. Any single extractor fails on some
layouts; three with different failure modes get usable text off far more pages
than the best one alone. Whatever produced the text is recorded, so a site that
only ever falls through to the crude tier is visible rather than silently
producing mush.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

try:
    import trafilatura
    from trafilatura.settings import use_config
    _TRAF_CONFIG = use_config()
    # Default is 10s per document, which is generous on modest hardware and
    # occasionally hangs outright on pathological markup.
    _TRAF_CONFIG.set("DEFAULT", "EXTRACTION_TIMEOUT", "6")
    HAVE_TRAFILATURA = True
except Exception:
    HAVE_TRAFILATURA = False
    _TRAF_CONFIG = None

MIN_USEFUL_CHARS = 220

# lxml refuses to build a tree containing NULLs or stray control characters and
# raises deep inside drop_tree(), which surfaced as a noisy traceback during
# backfill. Real pages do carry these -- truncated responses, mis-declared
# encodings, embedded binary. Stripping them costs nothing: none of these are
# meaningful in article text.
# XML 1.0 permits #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] |
# [#x10000-#x10FFFF]. Everything outside that makes lxml raise, and readability
# parses through lxml, so one bad codepoint kills the whole extractor.
#
# The C0 set alone was not enough. Pages whose declared encoding disagrees with
# their bytes decode to lone SURROGATES (\ud800-\udfff), which survive a
# C0-only filter and then blow up deep inside lxml_html_clean as "All strings
# must be XML compatible". Observed live during the 16-week backfill: twice in
# the first six weeks, each one a page whose text was silently lost.
_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]"
)


def _strip_control_chars(html: str) -> str:
    return _CONTROL_CHARS.sub("", html or "")

# Boilerplate that survives naive extraction and pollutes evidence summaries.
_JUNK_LINE = re.compile(
    r"^\s*(share this|advertisement|sign up|subscribe|read more|related stories|"
    r"cookie|accept all|newsletter|follow us|©|all rights reserved)\b",
    re.IGNORECASE,
)


def _tidy(text: str, limit: int | None = None) -> str:
    lines = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or _JUNK_LINE.match(line):
            continue
        lines.append(line)
    out = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if limit and len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0].strip()
    return out


def _via_trafilatura(html: str, url: str) -> dict:
    if not HAVE_TRAFILATURA:
        return {}
    try:
        # favor_precision is NOT set, and that is a measured decision. It cost
        # 5-7% of the text on every page tried -- pv-magazine 1842 against
        # 1905, Wikipedia 5551 against 5726, tomshardware 14771 against 15541,
        # semiwiki 3333 against 3798 -- and the share of characters living in
        # sentence-length lines was identical either way, so what it dropped
        # was article rather than furniture.
        #
        # favor_recall is not set either: it returned byte-identical output to
        # the default on every page, so it buys nothing and only adds a way for
        # a future trafilatura to behave differently.
        #
        # include_tables stays on because tables are where the numbers are, and
        # a ledger of cited figures cannot afford to drop them. include_comments
        # stays off because comment threads are the single largest source of
        # off-topic text on the pages this fetches.
        text = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=True,
            config=_TRAF_CONFIG,
        )
        if not text or len(text) < MIN_USEFUL_CHARS:
            return {}
        meta = {}
        try:
            doc = trafilatura.extract_metadata(html, default_url=url)
            if doc:
                meta = {
                    "title": doc.title or "",
                    "published": doc.date or "",
                    "author": doc.author or "",
                    "sitename": doc.sitename or "",
                }
        except Exception:
            meta = {}
        return {"text": text, "tier": "trafilatura", **meta}
    except Exception:
        return {}


def _via_resiliparse(html: str, url: str) -> dict:
    """C++/Cython, built for Common Crawl. Sixteen times faster than tier one.

    Higher recall than trafilatura and correspondingly more boilerplate, which
    is the right trade for a SECOND rung: it only runs when the careful one has
    already declined, and some text with a bit of furniture beats none.

    Its `main_content` heuristic is decisive rather than cautious -- on a few
    layouts it locks onto the wrong block and returns almost nothing. That is
    survivable here because there is another rung underneath, and it is exactly
    why this is not tier one.
    """
    try:
        from resiliparse.extract.html2text import extract_plain_text
    except ImportError:
        return {}
    try:
        text = extract_plain_text(html, main_content=True, list_bullets=False,
                                  alt_texts=False, links=False, form_fields=False)
        if not text or len(text) < MIN_USEFUL_CHARS:
            return {}
        return {"text": text, "tier": "resiliparse", "title": _title_of(html)}
    except Exception:
        return {}


def _via_selectolax(html: str, url: str) -> dict:
    """The floor. A very fast HTML parser with the obvious furniture removed.

    Deliberately not clever. Everything above this has already tried to find
    the article and failed, so the useful thing to do is return the page's text
    without the parts that are certainly not article -- and to do it in about
    two milliseconds rather than the thirty-seven BeautifulSoup was taking to
    return two thirds boilerplate.
    """
    try:
        from selectolax.lexbor import LexborHTMLParser
    except ImportError:
        return {}
    try:
        tree = LexborHTMLParser(html)
        for tag in ("script", "style", "nav", "header", "footer", "aside",
                    "form", "noscript", "svg", "iframe", "template"):
            for node in tree.css(tag):
                node.decompose()
        body = tree.body
        if body is None:
            return {}
        text = body.text(separator="\n", strip=True)
        if not text or len(text) < MIN_USEFUL_CHARS:
            return {}
        title = tree.css_first("title")
        return {"text": text, "tier": "selectolax",
                "title": title.text(strip=True) if title else ""}
    except Exception:
        return {}


def _title_of(html: str) -> str:
    """The <title>, cheaply. Tier one returns metadata of its own; the faster
    rungs do not, and a result without a title is noticeably worse to read."""
    try:
        from selectolax.lexbor import LexborHTMLParser
        node = LexborHTMLParser(html).css_first("title")
        return node.text(strip=True) if node else ""
    except Exception:
        return ""


def extract(html: str, url: str = "", *, max_chars: int = 3500) -> dict:
    """Return {ok, text, tier, title, published, chars, reason}."""
    if not html or len(html) < 200:
        return {"ok": False, "text": "", "tier": None, "reason": "empty_html",
                "title": "", "published": "", "chars": 0}

    html = _strip_control_chars(html)
    for extractor in (_via_trafilatura, _via_resiliparse, _via_selectolax):
        try:
            result = extractor(html, url)
        except Exception:
            # An extractor blowing up must not end the cascade -- that is the
            # entire reason there are three of them.
            result = {}
        if not result:
            continue
        text = _tidy(result["text"], limit=max_chars)
        if len(text) < MIN_USEFUL_CHARS:
            continue
        return {
            "ok": True,
            "text": text,
            "tier": result["tier"],
            "title": result.get("title", ""),
            "published": result.get("published", ""),
            "author": result.get("author", ""),
            "chars": len(text),
            "reason": "",
        }

    return {"ok": False, "text": "", "tier": None, "reason": "no_extractor_succeeded",
            "title": "", "published": "", "chars": 0}


def links(html: str, url: str = "", *, max_chars: int = 3500) -> str:
    """The page's text with its anchors kept, as markdown links.

    A separate function rather than a flag on extract() because the two want
    opposite things. extract() is hunting for the article and throwing the
    navigation away; this is for callers who came FOR the navigation -- link
    discovery, crawl frontiers, "what does this index page point at". Running
    the article extractors first and re-attaching hrefs afterwards would mean
    parsing twice to recover exactly what the first parse set out to discard.

    Relative hrefs are resolved against the page, so a caller can fetch what
    comes back without knowing where it came from. Returns "" rather than
    raising: this is a degraded-quality answer at worst, never a failed
    request, and the caller already has the prose from the normal path.
    """
    try:
        from selectolax.lexbor import LexborHTMLParser
    except ImportError:
        return ""
    try:
        tree = LexborHTMLParser(html)
        for tag in ("script", "style", "nav", "header", "footer", "aside",
                    "form", "noscript", "svg", "iframe", "template"):
            for node in tree.css(tag):
                node.decompose()
        body = tree.body
        if body is None:
            return ""
        for anchor in body.css("a"):
            href = (anchor.attributes.get("href") or "").strip()
            text = anchor.text(strip=True)
            if not href or not text:
                continue
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            anchor.replace_with("[%s](%s)" % (text, urljoin(url, href)))
        # Same de-duplication the article path does, for the same reason: a
        # nav rendered twice for mobile and desktop is one link, not two.
        seen = {}
        for line in body.text(separator="\n", strip=True).splitlines():
            line = line.strip()
            if line:
                seen.setdefault(line, None)
        return "\n".join(seen)[:max_chars]
    except Exception:
        return ""


def available() -> dict:
    """Which rungs of the cascade this installation actually has.

    selectolax is reported rather than assumed: it is the floor, so if it is
    missing the cascade has no last resort and a trafilatura failure becomes a
    failed extraction rather than a scruffy one.
    """
    def have(module):
        import importlib.util
        return importlib.util.find_spec(module) is not None

    return {
        "trafilatura": HAVE_TRAFILATURA,
        "resiliparse": have("resiliparse"),
        "selectolax": have("selectolax"),
    }
