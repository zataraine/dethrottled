# Architecture

How the pieces fit together. For the reasoning and the measurements behind each
decision, see [TLDREADME.md](TLDREADME.md).

## The request path

```
query
  ├─ web search (duckduckgo, bing)   ┐
  ├─ Bing News RSS                   ├─ pooled, deduped on canonical URL
  ├─ SearXNG          (optional)     │  and on title
  └─ Google News RSS                 ┘
        │
        ├─ + corpus passages the web did not return
        ├─ BM25 over title + first 240 characters
        ├─ cross-encoder reranks the top 40
        ├─ known-unreadable domains moved to the back of the fetch queue
        │
        └─ fetch only the winners
              ├─ video URL? → captions, before any network
              └─ ladder: direct → tls → crawl4ai
                    │
                    ├─ PDF      → pymupdf → OCR if no text layer
                    ├─ document → the parser for that signature
                    └─ HTML     → trafilatura → resiliparse → selectolax
                          │
                          └─ cached 21 days, indexed into the corpus
```

Three orderings are deliberate:

- **Rank before fetching.** Fetching costs seconds, ranking costs microseconds,
  and ranking does not need page bodies.
- **Merge the corpus before ranking.** Otherwise every local hit is silently
  declared worse than every web hit.
- **Check for video first.** No tier extracts prose from a player.

## Modules

```
src/dethrottled/
  server.py     the HTTP API. Routes, request shapes, background indexing
  search.py     web + RSS + SearXNG, engine resting, dedup, the sweep
  fetch.py      the tier ladder, robots, budgets, cooldown, canonical_url
  extract.py    trafilatura → resiliparse → selectolax
  documents.py  PDF/Office/ODF/EPUB/RTF/CSV, dispatched by file signature
  ocr.py        scanned PDFs via Tesseract, piped as PNG on stdin
  media.py      video URLs → their caption track
  corpus.py     passages, embeddings, the relevance floor, retention
  rank.py       BM25, corpus merge, cross-encoder
  domains.py    learned per-domain fetchability
  cache.py      SQLite. search 6h, bodies 21d, robots 24h
  health.py     capability probes
  paths.py      where state lives
  _quiet.py     third-party noise we have checked and understood
```

**Import direction matters.** `search` imports `fetch`; `rank` imports
`corpus`; `server` imports everything. Anything shared by `search` and
something above it belongs in `fetch` — putting `canonical_url` higher up once
created a cycle that only appeared when a third module imported it.

## The API

Two verbs and one combination, not one endpoint per tier.

| endpoint | |
| --- | --- |
| `POST /search` | a query → ranked results |
| `POST /fetch` | URLs → text (`raw: true` for source) |
| `POST /search-and-fetch` | both, fetching only the winners |
| `POST /extract`, `/search-and-extract` | aliases |
| `GET /corpus/search`, `/corpus/stats` | query what is already held |
| `GET /health`, `/ready` | liveness |
| `GET /v2/status` | **capability**, not reachability |
| `GET /v2/capabilities` | what is configured, not what is possible |
| `GET /stats` | budgets, resting tiers, domain health |

There is deliberately **no `/crawl`**. Rendering is a strategy for obtaining a
page, not something a caller wants for its own sake — the ladder escalates by
itself, and a caller choosing the renderer by hand would spend four seconds of
Chromium on a page `direct` serves in two hundred milliseconds. Forcing it is
the `render` parameter.

## The fetch ladder

> **A tier succeeds only if it yields readable prose.**

A JavaScript-only page returns HTTP 200, a complete body, and zero characters
of article text. A ladder that escalates on failed *fetches* declares victory
there. So escalation is driven by recovered text, and anything under
`THIN_CHARS` (600) counts as a miss.

| tier | speed | needs | notes |
| --- | --- | --- | --- |
| `direct` | ~2.1s | nothing | requests + robots. Solves most pages |
| `tls` | ~0.3s | a library | real Chrome TLS fingerprint, no browser |
| `crawl4ai` | ~4.4s | a container | renders JavaScript, locally |
| `jina-reader` | ~4.8s | internet | optional, **off by default** — leaves your network |

`tls` sits second because it is **faster than `direct`** (310ms vs 536ms
median) — curl-impersonate is C. It is a quick fallback, not a slow one.

`crawl4ai` speaks two dialects (`POST /crawl` upstream, `POST /render` for a
hand-rolled worker) and probes once, because guessing wrong costs a *silently
dead tier* rather than a loud error.

There is **no relay tier**: every option needs an account, a third party who
learns your URLs, or an address range that is pre-blocklisted.

## Budgets and politeness

- whole-page budget checked **between** tiers, never during one — a tier
  already running finishes, because killing a fetch about to succeed wastes
  everything spent on it. 60s named, 25s bulk
- the `tls` tier has its own 8s timeout, so it cannot spend the budget
  belonging to the renderer
- hourly volume budgets per tier, surfaced by `/stats`
- robots.txt honoured at every tier and cached; one request per domain at a
  time with a 1.5s floor; honest User-Agent; 10MB ceiling

**Tier cooldown**: a tier that refuses is rested, doubling to a one-hour cap,
cleared on success. **Only shared services rest** — `crawl4ai` and
`jina-reader`. `direct` and `tls` are per-host operations, and resting them
globally on one 403 dropped extraction from 87% to 67%.

## Content routing

Two decisions, neither using a model.

**Video** is decided from the URL before any network: a host allowlist plus an
11-character ID pattern. 0.9–2.9µs.

**Format** is decided from the file **signature**, never the server's header —
an HTML error page served as `application/vnd.ms-excel` is routine. OOXML,
OpenDocument and EPUB are all zips, so the directory is read to see which: a
member path for Office, a `mimetype` member for the other two. 0.6µs for HTML,
9–27µs for zips.

CSV is the one format with no signature and needs a content-type or extension
hint; guessing would make every HTML page a one-column CSV.

## Extraction

trafilatura → resiliparse → selectolax, ordered by measurement on ten real
pages scoring both recovery and boilerplate:

| extractor | median ms | junk % |
| --- | --- | --- |
| resiliparse | 2.0 | 5.5% |
| selectolax | 2.6 | 3.1% |
| trafilatura | 33.4 | **0.9%** |
| *readability* | *27.4* | *1.5%* — dropped, often returned almost nothing |
| *BeautifulSoup* | *37.2* | *66.5%* — dropped, worst on both axes |

trafilatura leads on quality; resiliparse is second because it is 16× faster
and catches real trafilatura misses; selectolax is the floor — not a content
extractor, a fast parser with the furniture removed.

Documents get their own path (§ documents.py) and scanned PDFs go to Tesseract
at 150dpi with one detected language, both of which were measured against the
alternatives.

## Ranking

Three optional stages, each degrading to the one before.

1. **BM25** on title + first 240 characters. Chosen over a bi-encoder on
   measurement: more useful rows in the top 14, and no time at all rather than
   167 seconds. Ranking on 240 characters beat ranking on 3,000.
2. **Corpus merge** — already-fetched passages compete with web results.
3. **Cross-encoder** over a 40-row shortlist. MRR 0.646 → 0.833 for 16ms.
   English-only, for licensing and weight reasons.

Recency is multiplicative and bounded: it may reorder relevant results and may
never promote an irrelevant one. Undated rows count as neutral.

Every response reports **which stages actually ran**.

## The corpus

Overlapping passages, embedded with all-MiniLM-L6-v2 (87MB, 384-dim), stored in
SQLite. The title rides on every passage.

No vector database: brute-force cosine is 2.5ms at 50,000 passages. The cap is
really a memory budget — 200,000 passages is a 307MB resident matrix, most of a
2GB Pi, so the default is 50,000.

The matrix is **appended to**, not rebuilt, on write. Rebuilding cost a second
at 200k and was paid after every fetch.

The relevance floor (0.22) is what stops it answering a question it has nothing
for — cosine always returns a best match. Real answers score 0.375–0.585 and
non-answers 0.03, so the floor sits in a wide empty band.

## Domain fetchability

Ranking answers *"is this relevant?"* and has no idea whether a page can be
**read**. `domains.py` records per-domain extraction outcomes and is consulted
at exactly one point: choosing which already-ranked results to spend a fetch
on. It never reorders by relevance and never drops a result.

Measured, not declared — no blocklist ships. Laplace-smoothed, needs 5
attempts, 14-day half-life so sites recover on their own.

## Health

Every probe does real work. Five states, and two of them carry the value:
**DEGRADED** (answered, returned nothing usable — the state that hid two
multi-day outages) and **OFF** (not configured, so it has not failed).

An unconfigured source reports `not_configured`, never `down`. Only one of
those sends you to the right place.

## Concurrency

Sync `def` routes in FastAPI's threadpool — 100 concurrent clients show no
meaningful degradation. Async everywhere was measured as unnecessary and
probably harmful: `async def` calling `requests`, `sqlite3` and `onnxruntime`
would stall the event loop.

All shared state is locked: cache, budgets, throttle, cooldown, domain health,
corpus matrix, and engine health — the last of which zeroed itself under 32
threads before it was.
