#!/usr/bin/env python3
"""An HTTP API over the free stack: search, fetch, extract.

Six routes, no keys, no quota, no shared pool. Point anything that speaks HTTP
at it and it works:

    POST /search              free search across every configured source
    POST /fetch               URLs -> their prose (raw=true for the source)
    POST /search-and-fetch    both at once, fetching only the winners
    GET  /corpus/search       what has already been fetched, no network
    GET  /health              is the process alive
    GET  /v2/status           which sources and extractors actually work
    GET  /v2/capabilities     what this build can do

Two response fields are worth knowing about because most search APIs do not
give you them. `search_attempts` reports which engine returned how many rows
and which ones did not answer at all, so a caller can tell a genuinely empty
result from a silently degraded one. `tier` says HOW a page was obtained --
`direct/trafilatura`, `crawl4ai/readability` -- which is the difference between
trusting a result and knowing why you got it.

Deliberately NOT included: any editorial opinion about which sources are good.
The reputable-domain sweep and the preferred-domain ranking are available and
are driven entirely by lists the CALLER supplies. A search service that decides
for you what counts as a real publisher is a search service you cannot use for
a subject it was not built for.

Two verbs and one combination, not one endpoint per tier. There is
deliberately no `/crawl`: rendering is a STRATEGY for obtaining a page, not
something a caller wants for its own sake. The ladder escalates to the renderer
by itself when the cheap tiers fail, and a caller choosing it by hand would
spend four seconds of Chromium on pages `direct` serves in two hundred
milliseconds. Where forcing the question is genuinely useful, that is the
`render` parameter, not a route.

`/extract` and `/search-and-extract` remain as aliases: they are what earlier
callers were written against, and renaming for tidiness is a poor trade. So is
`/extract-with-links`, which is `/fetch` with `links: true` -- the anchors kept
and rendered as markdown, for callers doing link discovery rather than reading.

Run:
    dethrottled --port 8182
    python -m dethrottled.server --port 8182
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel, ConfigDict, Field

# Read from the package, never restated here.
#
# This was hardcoded to "0.1.0" while pyproject.toml and __init__.py both said
# 0.1.2, so /health and /v2/capabilities reported a version the software had
# not been for two releases. That is the number anyone evaluating this actually
# sees, and a reviewer duly wrote it up as "version 0.1.0 -- early".
#
# Three places claiming a version is two too many.
from . import __version__ as VERSION
from . import domains as domain_health
from . import extract as fx
from . import fetch as fetcher
from . import paths as _paths
from . import rank as ranker
from . import search as fs
from .cache import Cache
from .corpus import index_fetched

STARTED = time.time()

_cache = None


def cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache(Path(os.environ.get(
            "DETHROTTLED_CACHE",
            str(_paths.data_dir() / "cache.sqlite"))))
    return _cache


app = FastAPI(title="dethrottled", version=VERSION,
              description="Zero-API search, fetch and extraction")


class SearchBody(BaseModel):
    # Unknown fields are rejected rather than ignored, so a caller that mistypes
    # one hears about it instead of silently getting the default.
    model_config = ConfigDict(extra="forbid")

    query: str
    # The contract's name for result count. num_results and max_items remain
    # accepted for callers already written against them; limit wins.
    limit: int | None = None
    num_results: int = 8
    max_items: int | None = None
    categories: str = ""
    # Honoured, by the SearXNG source. Worth about as much as translating the
    # query: with it a French query returns 108 rows and 57 useful against 66
    # and 28 without. The other sources are English-centric and take no such
    # hint, so this biases the pool rather than constraining it.
    language: str = ""
    engines: str = ""
    # Honoured: bypasses the search cache for this call. It was accepted and
    # ignored until an external benchmark set it, measured this engine's cache
    # against another engine doing live work, and published the wrong
    # conclusion. Accepting a field costs nothing right up until someone
    # believes it.
    fresh: bool = False
    profile: str = "balanced"

    # Ranking. `rank` is BM25 and free, so it is on. `rerank` loads a model and
    # costs per document, so it is not -- but when it is on it is applied to a
    # shortlist only, which is what makes it affordable at all.
    rank: bool = True
    rerank: bool = False
    # How many already-fetched corpus passages to merge into the pool before
    # ranking. 0 disables it. These cost no fetch, so they are cheaper than the
    # web rows they compete with, not merely additional.
    corpus: int = 0
    # 0.0 = pure relevance, 1.0 = freshness dominates. Bounded and
    # multiplicative: it may reorder relevant results and may never promote an
    # irrelevant one.
    recency: float = 0.0


class FetchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(default_factory=list)
    # What the ladder is ALLOWED to do, not what it must do. "never" keeps it
    # on the cheap local tiers for callers to whom latency matters more than
    # coverage.
    # auto    escalate to the renderer only when cheaper tiers fail (default)
    # always  try the renderer FIRST, for a page you know needs a browser
    # never   stay on the cheap local tiers
    # auto | always | never. A bool is also accepted and mapped, because
    # callers were written against one -- and because typing this field
    # differently on two engines is what returned 422 on every fetch through a
    # bridge for a day. The enum is canonical; True means always, False auto.
    render: str | bool = "auto"
    # text   article prose (default)
    # links  prose with anchors kept as markdown, for link discovery
    # html   the source, for callers doing their own parsing
    #
    # Replaces the raw/links booleans, which could contradict each other and
    # left a caller no way to say "either one" without knowing which won.
    format: str = "text"
    # The prose is the point; `raw` is for callers doing their own parsing, and
    # is the only reason this is a distinct verb rather than a rename. Kept
    # because callers send it; `format` wins when both appear.
    raw: bool = False
    # Keep the anchors, rendered as markdown links. For callers doing link
    # discovery rather than reading: an index page's VALUE is its outbound
    # links, and the article extractors throw those away by design.
    links: bool = False
    # 8000, not 3500: 3,500 characters is about 550 words, and a news article
    # is 500 to 800 -- this endpoint was truncating typical articles. Not
    # 10,000, because past roughly eight thousand most sites are into
    # related-articles and footer.
    max_chars: int = 8000
    fresh: bool = False
    profile: str = "balanced"


class SearchFetchBody(SearchBody):
    # auto | always | never. A bool is also accepted and mapped, because
    # callers were written against one -- and because typing this field
    # differently on two engines is what returned 422 on every fetch through a
    # bridge for a day. The enum is canonical; True means always, False auto.
    render: str | bool = "auto"
    raw: bool = False
    # 3000. Leaner than /extract on purpose: this path multiplies by the number
    # of results, and it is the ranking path, where measurement says less text
    # ranks better -- titles plus 240 characters beat 3,000 characters of body.
    # 3000 is the measured sweet spot for the ranking path.
    max_chars: int = 3000


def _attempts(meta: dict) -> list:
    """Per-engine telemetry: who answered, with how many rows, how fast.

    Callers use this to tell an honestly empty result from a silently degraded
    one. An engine that has been CAPTCHA-ed out contributes zero rows and no
    error, which looks exactly like a query nobody has written about.
    """
    rows = []
    for name, count in (meta.get("per_source") or {}).items():
        # A source can report a row count or the string "not_configured".
        # Both become a count here, because callers do arithmetic on this
        # field -- but the distinction survives in `status`, which is the
        # whole reason the source bothered to say so.
        configured = not isinstance(count, str)
        rows.append({"engine": name,
                     "count": count if configured else 0,
                     "status": "ok" if configured else count,
                     "elapsed_ms": meta.get("elapsed_ms", 0),
                     "unresponsive": []})
    return rows


def _search_row(row: dict, meta: dict, index: int) -> dict:
    return {
        "url": row.get("url", ""),
        "title": row.get("title", ""),
        "snippet": row.get("snippet", ""),
        "publishedDate": row.get("publishedDate") or None,
        "engine": row.get("engine", "dethrottled"),
        "engines": [row.get("engine", "dethrottled")],
        "category": None,
        "score": None,
        "cached": False,
        "search_attempts": _attempts(meta) if index == 0 else [],
        "search_elapsed_ms": meta.get("elapsed_ms", 0),
        # Which ranking stages actually ran -- not which were requested. Asking
        # for a cross-encoder that is not installed gets you lexical ordering,
        # and you should be able to see that here rather than infer it from
        # disappointing results.
        "ranking": meta.get("ranking", []) if index == 0 else [],
        "from_corpus": bool(row.get("from_corpus")),
    }


MIME = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "docx": ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document"),
    "pptx": ("application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation"),
    "html": "text/html",
}


def _render_mode(render) -> str:
    """auto | always | never, whichever spelling arrived.

    The enum is canonical and a bool is a documented alias, because this field
    was typed differently on two engines and the mismatch returned 422 on every
    fetch through a bridge for a day. Accepting both on both is what makes them
    agree without breaking either engine's callers.
    """
    if isinstance(render, bool):
        return "always" if render else "auto"
    return render


def _extract_row(url: str, max_chars: int, allow_ocr: bool = True,
                 page_budget: float | None = None, allow_render: bool = True,
                 raw: bool = False, render_first: bool = False,
                 links: bool = False, fresh: bool = False) -> dict:
    # allow_ocr is the one-URL-versus-many split. OCR costs about 1.4s a page,
    # which is worth it for a page somebody asked for by name and is not worth
    # it multiplied across a page of search results.
    # fresh means fresh. The cache is a parameter, so bypassing it is simply
    # not passing one -- and a field that says it bypasses the cache must, or
    # a caller cannot tell what they measured.
    result = fetcher.fetch_and_extract(url, max_chars=max_chars,
                                       cache=None if fresh else cache(),
                                       allow_ocr=allow_ocr,
                                       page_budget=page_budget,
                                       allow_render=allow_render,
                                       render_first=render_first,
                                       keep_html=raw or links)
    # Record what happened, per domain. A cache hit is not evidence about the
    # site -- it says the cache worked, which is a different fact -- so only
    # live attempts count.
    if not result.get("cached"):
        domain_health.record(url, result["ok"])

    if not result["ok"]:
        return {"url": url, "content": "", "content_type": None,
                "quality": "failed", "failure_reason": result["reason"][:160],
                # fetch_and_extract now reports which tier it was last
                # standing on when it gave up; a hardcoded None here would
                # throw that away and leave a tier-only consumer with nothing.
                "tier": result.get("tier"), "cached": result.get("cached", False)}
    row = {
        "url": url,
        "content": result["text"],
        "content_type": MIME.get(result.get("content_type", ""), "text/html"),
        "quality": "ok",
        # `tier` says HOW the content was obtained: which fetch tier, which
        # extractor. "It worked" and "it worked on the fourth try through a
        # renderer" are different levels of confidence in the same text.
        "tier": "%s/%s" % (result["tier"], result["extractor"]),
        "cached": result.get("cached", False),
        "title": result.get("title", ""),
        "published": result.get("published", ""),
        # Only present when the caller asked for it. Absent, not empty, so a
        # client can distinguish did-not-ask from asked-and-got-nothing.
        **({"html": result.get("html", "")} if raw else {}),
    }
    if links:
        # Falls back to the prose on an empty result rather than handing
        # back nothing: a page whose anchors could not be parsed is still
        # a page the caller asked to read.
        annotated = fx.links(result.get("html", ""), url, max_chars=max_chars)
        if annotated:
            row["content"] = annotated
    elif raw and result.get("html"):
        # format=html asked for the source, so the source is the content. It is
        # still echoed in `html` for callers written against raw=true.
        row["content"] = result["html"][:max_chars]
    return row


@app.get("/health")
def health():
    return {"status": "ok", "service": "dethrottled", "version": VERSION}


@app.get("/ready")
def ready():
    report = fs.health()
    return {"status": "ok" if report["ok"] else "degraded", **report}


@app.get("/v2/status")
def v2_status():
    report = fs.health()
    return {
        "status": "ok" if report["ok"] else "degraded",
        "components": {
            # An unconfigured source is not a down source: "down" sends you
            # looking for a broken service, "not_configured" sends you to the
            # settings, and only one of those is where the problem is.
            "searxng": {"status": "ok" if report["searxng"]
                        else ("down" if fs.SEARXNG_URL else "not_configured")},
            "bing_news_rss": {"status": "ok" if report["bing_news"] else "down"},
        },
        "providers": {k: {"configured": v}
                      for k, v in fx.available().items()},
        "profiles": {p: {"ready": True} for p in ("fast", "balanced", "thorough")},
        "cost": {"api_calls": 0, "daily_caps": None, "keys_required": False},
        "uptime_seconds": int(time.time() - STARTED),
    }


@app.get("/v2/capabilities")
def v2_capabilities():
    return {
        "service": "dethrottled",
        "version": VERSION,
        # What is CONFIGURED, not what the code is capable of. Listing searxng
        # on a host with no SearXNG is the same class of lie as a health check
        # that reports ok because a socket opened.
        "search": [name for name, on in (
            # General web, via whichever keyless engines still answer. Listed
            # by the engines actually configured, because "web search" is not
            # one source and a caller debugging an empty result wants to know
            # which of them was even asked.
            *(("web-%s" % e, True) for e in fs.WEB_ENGINES),
            ("searxng-multi-engine", bool(fs.SEARXNG_URL)),
            ("bing-news-rss", True),
            ("google-news-rss", True)) if on],
        # In ladder order, and only what is actually switched on. Listing
        # jina-reader here while DETHROTTLED_ENABLE_JINA=0 was a lie of exactly
        # the kind /v2/status exists to avoid.
        "fetch_tiers": [name for name, on in (
            ("direct", True),
            ("tls", fetcher.ENABLE_TLS),
            ("crawl4ai", bool(fetcher.CRAWL4AI_URL)),
            ("jina-reader", fetcher.ENABLE_JINA),
        ) if on],
        "tiers_resting": fetcher.tier_rest_state(),
        "extract": [k for k, v in fx.available().items() if v],
        "ranking": ranker.available(),
        "quotas": None,
        "keys_required": False,

        # Policy, so a caller can choose an engine on what it will and will not
        # do rather than discovering it from a refusal.
        #
        # robots.txt is honoured at every tier and cached, which is a real
        # difference from engines that do not check it: some URLs that fetch
        # elsewhere will be refused here, and that is the intended behaviour
        # rather than a gap.
        "respects_robots": True,
        # Every tier here is free and keyless, so a call costs a caller nothing
        # and no budget can be exhausted on their behalf.
        "metered_tiers": False,
        # The external reader is the only tier that would send a URL to a third
        # party, and it is off unless explicitly enabled.
        "leaves_network": bool(fetcher.ENABLE_JINA),
        # What `fresh` bypasses.
        "cache_ttl_seconds": 6 * 3600,

        # Beyond the core contract fields, which every engine takes. A client
        # sends core + whatever is listed here, so one client works against
        # engines that differ without knowing which it is talking to.
        # Every field accepted beyond the core, and nothing that is not. An
        # under-declared field is as bad as an over-declared one: a client that
        # trusts this drops a refinement the engine would have used.
        "optional_fields": {
            # Only fields this engine actually honours. `engines` and
            # `profile` are accepted for compatibility but change nothing --
            # engines would reach one of five sources, profile is read
            # nowhere -- and a declared field is a promise that sending it
            # does something.
            "search": ["categories", "language", "rank", "rerank",
                       "corpus", "recency"],
            "fetch": ["raw", "links"],
        },
    }


def _ranked(body) -> tuple:
    """Search, then order the pool BEFORE anything expensive happens to it.

    The whole point of ranking here rather than in the caller is that fetching
    is the costly step: order first, fetch only the winners. A caller that
    ranks after fetching has already paid for every page it is about to throw
    away.

    Over-fetches the pool on purpose when ranking is on. Ranking `limit` rows
    and returning `limit` rows is not ranking, it is sorting -- there has to be
    something for the ranker to reject.
    """
    limit = body.limit or body.max_items or body.num_results
    pool = limit * 3 if (body.rank or body.rerank) else limit
    rows, meta = fs.search(body.query, max_items=pool,
                           categories=body.categories,
                           language=body.language or None,
                           # Same again: fresh bypasses the six-hour search
                           # cache rather than being accepted and ignored.
                           cache=None if body.fresh else cache())
    rows, stages = ranker.apply(
        rows, body.query, bm25=body.rank, rerank=body.rerank,
        corpus=body.corpus, recency=body.recency)
    # AFTER ranking, and only here. Relevance order is decided above and is
    # not touched; this moves domains that measurably never yield text to the
    # back of the queue, so the fetches about to be spent land on results that
    # can actually be read. Nothing is dropped -- if the whole pool is poor
    # domains, they are still what comes back.
    ordered = domain_health.order_for_fetching(rows)
    if ordered[:limit] != rows[:limit]:
        stages.append("fetchability")
    rows = ordered

    meta = dict(meta, ranking=stages)
    return rows[:limit], meta, limit


@app.post("/search")
def search(body: SearchBody):
    rows, meta, _limit = _ranked(body)
    return [_search_row(r, meta, i) for i, r in enumerate(rows)]


def _harvest(rows) -> list:
    """(url, row) pairs worth indexing, from API rows."""
    return [(r.get("url", ""), r) for r in rows
            if r.get("quality") == "ok" and r.get("content")]


@app.post("/fetch")
@app.post("/extract")            # alias, for callers written against the old name
def fetch_urls(body: FetchBody, background: BackgroundTasks):
    # Named URLs: try hard, OCR included.
    # format is the contract's single name; raw/links remain accepted for
    # callers written against them, and format wins when both are given.
    # Both spellings converge here, so nothing downstream has to know which
    # arrived.
    mode = _render_mode(body.render)
    want_html = body.format == "html" or (body.format == "text" and body.raw)
    want_links = body.format == "links" or (body.format == "text" and body.links)
    rows = [_extract_row(u, body.max_chars, allow_ocr=True,
                         allow_render=mode != "never",
                         render_first=mode == "always",
                         raw=want_html, links=want_links,
                         fresh=body.fresh)
            for u in body.urls if u]
    # Indexed AFTER the response, not during it. Embedding costs ~250ms a page
    # and the caller should not wait for work they did not ask for.
    # Not indexed when links were kept: the corpus is prose to match
    # against later, and this output has markdown link syntax woven
    # through it. Feeding it in makes every future match slightly worse.
    if not body.links:
        background.add_task(index_fetched, _harvest(rows))
    return rows


@app.post("/extract-with-links")
def fetch_with_links(body: FetchBody, background: BackgroundTasks):
    """`/fetch` with the anchors kept. Same verb, different output shape.

    A route rather than only a parameter for the same reason `/extract` is
    still here: it is what existing callers were written against, and renaming
    for tidiness is a poor trade. `links: true` on /fetch does exactly this.

    Note this path does not use the page cache -- keeping the source HTML
    bypasses it, because caching twenty times the bytes to serve a minority of
    requests is the wrong trade for a cache whose whole point is many pages
    cheaply.
    """
    body.links = True
    return fetch_urls(body, background)


@app.post("/search-and-fetch")
@app.post("/search-and-extract")     # alias
def search_and_fetch(body: SearchFetchBody, background: BackgroundTasks):
    rows, meta, _limit = _ranked(body)
    out = []
    for index, row in enumerate(rows):
        merged = _search_row(row, meta, index)
        # Search results: no OCR. N results times 1.4s a page is a different
        # trade from one URL somebody asked for.
        sf_mode = _render_mode(body.render)
        extracted = _extract_row(row.get("url", ""), body.max_chars,
                                 allow_ocr=False,
                                 page_budget=fetcher.PAGE_BUDGET_BULK,
                                 allow_render=sf_mode != "never",
                                 render_first=sf_mode == "always",
                                 raw=body.raw)
        merged.update({k: v for k, v in extracted.items() if k != "url"})
        out.append(merged)
    # This is the high-volume route, so it is the one that actually grows the
    # corpus -- and it was growing it by nothing at all.
    background.add_task(index_fetched, _harvest(out))
    return out


@app.get("/corpus/search")
def corpus_search(q: str, limit: int = 10, floor: float | None = None):
    """Search what has already been fetched, without fetching anything.

    The index was being written on every extraction and read by nothing over
    HTTP, which made it a write-only store: pages went in, and the only way to
    get one back was to fetch it off the network again.

    `floor` is what stops this answering a question it has nothing for. Cosine
    similarity always returns a best match, and the best match against an empty
    subject is noise with a number next to it. Leave it unset to use the floor
    calibrated for whichever model is in use -- the two do not share a scale.
    """
    try:
        from .corpus import Corpus
        hits = Corpus().search(q, limit=limit, floor=floor)
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:200], "results": []}
    return {"ok": True, "count": len(hits), "results": hits}


@app.get("/corpus/stats")
def corpus_stats():
    try:
        from .corpus import Corpus
        return {"ok": True, "models": Corpus().stats()}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:200], "models": {}}


@app.get("/stats")
def stats():
    # Tier budgets are surfaced because their exhaustion used to be invisible:
    # they were lifetime counters that nothing reset, so crawl4ai stopped
    # rendering after 8 pages and jina after 25, silently, for the rest of the
    # process. They refill hourly now, and you can see how much is spent.
    return {"cache": cache().stats(), "version": VERSION,
            "tiers_resting": fetcher.tier_rest_state(),
            "domain_health": domain_health.stats(),
            "uptime_seconds": int(time.time() - STARTED),
            "tiers": fetcher.tier_stats(),
            "tier_budget_used": fetcher.budget_state(),
            "tier_budgets": {"crawl4ai": fetcher.CRAWL4AI_BUDGET,
                             "jina-reader": fetcher.JINA_READER_BUDGET,
                             "window_seconds": fetcher.BUDGET_WINDOW}}


def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get(
        "DETHROTTLED_HOST", "127.0.0.1"),
        help="loopback by default; see SECURITY.md before changing it")
    parser.add_argument("--port", type=int, default=int(os.environ.get(
        "DETHROTTLED_PORT", "8787")))
    jina = parser.add_mutually_exclusive_group()
    jina.add_argument("--jina", dest="jina", action="store_true", default=None,
                      help="allow the external r.jina.ai reader tier")
    jina.add_argument("--no-jina", dest="jina", action="store_false",
                      help="never contact r.jina.ai; local tiers only")
    args = parser.parse_args()
    if args.jina is not None:
        # Set before anything imports the value off the module.
        fetcher.ENABLE_JINA = args.jina
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
