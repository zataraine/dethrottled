# Usage

## Running it

```bash
dethrottled                        # 127.0.0.1:8787
dethrottled --port 9000
dethrottled --no-jina              # local tiers only, nothing leaves your network
python -m dethrottled.server       # same thing, no console script needed
```

Port precedence: `--port` beats `DETHROTTLED_PORT` beats `8787`. The compose
stack reads `DETHROTTLED_PORT` from `.env`.

It binds to loopback. Read [SECURITY.md](SECURITY.md) before changing that —
this service fetches arbitrary URLs for anyone who can reach it.

---

## HTTP API

Two verbs and one combination. There is no `/crawl`: rendering is a `render`
parameter, not an endpoint.

### `POST /search`

```json
{"query": "okapi bm25 ranking", "num_results": 8}
```

| field | default | meaning |
| --- | --- | --- |
| `query` | — | required |
| `num_results` | `8` | how many rows to return |
| `categories` | `""` | passed to SearXNG |
| `rank` | `true` | BM25. Free, so it is on |
| `rerank` | `false` | cross-encoder over the top 40. Needs `[rerank]` |
| `corpus` | `0` | merge this many already-fetched passages into the pool |
| `recency` | `0.0` | 0 = pure relevance, 1 = freshness dominates |

Each row:

```json
{
  "url": "...", "title": "...", "snippet": "...",
  "publishedDate": null, "engine": "web-duckduckgo",
  "cached": false, "from_corpus": false,
  "ranking": ["bm25", "cross-encoder"],
  "search_attempts": [
    {"engine": "web", "count": 8, "status": "ok", "elapsed_ms": 412},
    {"engine": "searxng", "count": 0, "status": "not_configured"}
  ],
  "search_elapsed_ms": 412
}
```

Two fields most search APIs do not give you:

- **`search_attempts`** — who answered, with how many rows. This is how you
  tell an honestly empty result from a silently degraded one. An engine that
  has been CAPTCHA-ed out contributes zero rows and no error, which looks
  exactly like a query nobody has written about. `status` distinguishes `ok`
  from `not_configured`.
- **`ranking`** — which stages actually ran, not which you asked for.

Both appear on the first row only; repeating them per row would multiply a
fixed cost by the result count for no added information.

### `POST /fetch`

```json
{"urls": ["https://example.com/article"], "max_chars": 8000}
```

| field | default | meaning |
| --- | --- | --- |
| `urls` | `[]` | one or many |
| `max_chars` | `8000` | per document |
| `render` | `"auto"` | `auto` or `never` — what the ladder may do |
| `raw` | `false` | also return the page source |

```json
[{"url": "...", "content": "...", "content_type": "text/html",
  "quality": "ok", "tier": "direct/trafilatura", "cached": false,
  "title": "...", "published": "..."}]
```

`tier` is `fetch-tier/extractor`. "It worked" and "it worked on the third try
through a renderer" are different levels of confidence in the same text.

A page that cannot be fetched is a row with `"quality": "failed"` and a
`failure_reason`, **not** an HTTP error — one bad URL in a batch must not lose
the whole response.

`raw: true` bypasses the cache, because the cache deliberately stores extracted
text and not source: HTML averages **47× the size of its text**, up to 182×.

OCR runs here (a URL asked for by name is worth 1.4s a page) but not in
`/search-and-fetch`, where it would multiply across every result.

### `POST /search-and-fetch`

Every field from both. Searches, ranks, then fetches **only the winners** —
which is the point of ranking first. `max_chars` defaults to `3000` here rather
than 8000, because this path multiplies by the result count.

`/extract` and `/search-and-extract` are aliases of these two.

### `GET /corpus/search`

```bash
curl -G localhost:8787/corpus/search \
  --data-urlencode 'q=how are documents ranked' --data 'limit=10'
```

Searches what you have already fetched, without fetching anything. `floor`
overrides the relevance threshold — leave it unset unless you know why: real
answers score 0.375–0.585 and questions the corpus cannot answer score around
0.03, so the default (0.22) sits in a very wide empty band.

### `GET /health`, `/v2/status`, `/v2/capabilities`, `/stats`

`/health` is liveness. **`/v2/status` is the honest one** — it does real work
and reports `degraded` when a tier answers but returns nothing usable.

`/v2/capabilities` reports what is *configured*, not what the code can do:

```json
{"search": ["web-duckduckgo", "web-bing", "bing-news-rss", "google-news-rss"],
 "fetch_tiers": ["direct", "tls", "crawl4ai"],
 "ranking": {"bm25": true, "corpus": true, "rerank": true},
 "tiers_resting": {}, "quotas": null, "keys_required": false}
```

`/stats` surfaces tier budget consumption, which tiers are resting and why, and
everything domain health has learned.

---

## Python API

```python
from dethrottled import fetch as f, rank, search as fs
from dethrottled.cache import Cache

rows, meta = fs.search("okapi bm25", max_items=8, cache=Cache("cache.sqlite"))
rows, stages = rank.apply(rows, "okapi bm25", rerank=True, corpus=5)

result = f.fetch_and_extract("https://example.com/a", max_chars=8000)
# {ok, text, tier, extractor, title, published, chars, url, reason, cached}

f.canonical_url("https://www.example.com/a?utm_source=x#top")
# 'https://example.com/a'

from dethrottled.corpus import Corpus
Corpus().search("what is term weighting", limit=5)

from dethrottled import media
media.transcript("https://youtu.be/VIDEOID")     # (text, title, reason)
```

---

## What it can read

Content type is decided from the file **signature**, never the server's header.

| kind | formats |
| --- | --- |
| web | HTML |
| documents | PDF (+OCR), DOCX, XLSX, XLS, PPTX, CSV/TSV |
| open formats | ODT, ODS, ODP, EPUB, RTF |
| video | YouTube captions |

Legacy `.doc`/`.ppt` are refused **by name**, not silently — reading them needs
a ~500MB converter.

---

## Configuration

Everything is an environment variable and nothing is required. The full
reference is in [TLDREADME §17](TLDREADME.md#17-configuration-reference); the
ones people actually change:

| variable | default | |
| --- | --- | --- |
| `DETHROTTLED_PORT` | `8787` | |
| `DETHROTTLED_DATA_DIR` | `~/.cache/dethrottled` | caches, corpus, health files |
| `DETHROTTLED_MODEL_DIR` | `<cache>/models` | does **not** follow DATA_DIR |
| `DETHROTTLED_SEARXNG_URL` | `""` | your instance; empty = skipped |
| `DETHROTTLED_CRAWL4AI_URL` | `""` | your renderer; empty = skipped |
| `DETHROTTLED_ENABLE_JINA` | `1` pip, `0` compose | **leaves your network** |
| `DETHROTTLED_ENABLE_TLS` | `1` | the curl_cffi tier |
| `DETHROTTLED_THIN_CHARS` | `600` | below this a result escalates |
| `DETHROTTLED_USER_AGENT` | honest default | say what you are |

---

## Docker

```bash
cp .env.example .env
docker compose up -d
curl localhost:8787/health
```

Brings up dethrottled + SearXNG + Crawl4AI. Nothing leaves your network: the
external reader is off, because the renderer covers JavaScript locally. SearXNG
and Crawl4AI are not published to the host at all.

```bash
docker compose logs -f dethrottled
docker compose down          # keeps the data volume
docker compose down -v       # deletes it
```

Running a renderer without Docker: `examples/crawl4ai_server.py` is a forty-line
server for the pip install of Crawl4AI, which ships the crawler but not the
HTTP service.

---

## Models

```bash
./scripts/fetch-models.sh
```

87MB, one model. The reranker downloads itself on first use (21MB). Both are
Apache-2.0; nothing non-commercial is used anywhere.

OCR language packs are separate:

```bash
./scripts/fetch-models.sh --all
```

---

## Benchmarking

```bash
python scripts/benchmark.py --url http://localhost:8787 --out results.json
```

Twenty awkward cases — five languages, JavaScript pages, PDFs, paywalls,
forums, tables. It uses the same public API you do.

**A caveat on methodology.** This benchmark has high run-to-run variance,
because free search engines return a different URL set every run. For comparing
two builds use `scripts/ab_extract.py`, which gives both the same fixed URL
list and measures extraction only. And check `cached` on both sides before
believing any comparison — see [TLDREADME §20](TLDREADME.md#20-reproducing-every-number).

---

## Things that will bite you

1. **`docker compose` needs `shm_size`.** Chromium needs more shared memory
   than Docker's 64MB default. Without it renders fail looking like timeouts
   rather than out-of-memory.

2. **SearXNG serves HTML only by default.** Without `formats: [html, json]` in
   `settings.yml` every API query returns 403 and the tier looks broken rather
   than misconfigured. The shipped config sets it.

3. **Fetching many URLs from one domain is slow by design.** 1.5s between
   requests to the same host: 8 URLs from one domain take 12.7s, 8 URLs from
   eight domains take 3.3s. That is politeness, not a defect.

4. **`raw: true` refetches.** The cache does not store source, deliberately.

5. **YouTube transcripts need a residential IP.** Datacentre ranges are blocked
   hard. A block is reported as `transcript_blocked_from_this_address`.

6. **Scraped search engines rot.** Three of eight tested were hard-blocked and
   stayed blocked with a four-second gap. Unhealthy engines rest 30 minutes and
   retry; the default list may need revisiting over time.

7. **Do not set `DETHROTTLED_DATA_DIR` and expect models to follow.** They
   deliberately do not — pointing the data directory at a scratch path would
   otherwise orphan the download.

8. **`0.0.0.0` is not a configuration option, it is a decision.** See
   [SECURITY.md](SECURITY.md).
