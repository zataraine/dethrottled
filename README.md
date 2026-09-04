# dethrottled

**Web search, fetch and extraction with no API keys, no accounts, and no quota.**

Ask it a question and it returns results. Give it a URL and it returns the text
— from a web page, a PDF, a spreadsheet, a slide deck, an ebook, or a video's
captions. It runs on your machine, and there is nothing to sign up for.

```bash
pip install 'dethrottled[all]'
dethrottled
```

```bash
curl -X POST localhost:8787/search-and-fetch \
  -H 'content-type: application/json' \
  -d '{"query": "okapi bm25 ranking", "num_results": 3}'
```

Or bring up the whole self-hosted stack — search engines, a JavaScript renderer
and the API — with nothing leaving your network:

```bash
docker compose up -d
```

> Want the whole story? [**TLDREADME.md**](TLDREADME.md) documents every tier,
> every measurement, and every decision in full. This page is the short version.

---

## Maintainer

This repository is owned and maintained by [@zataraine](https://github.com/zataraine).

## Why it exists

Most search and scraping tools ask for an API key, meter you, and cut you off.
The ones that don't usually fall over on the pages that matter — the JavaScript
app, the PDF with no text layer, the spreadsheet where the actual numbers live.

dethrottled handles those, locally, for nothing. Every number below is measured
and reproducible from scripts in this repository.

## Three endpoints

| endpoint | what it does |
| --- | --- |
| `POST /search` | a query → ranked results |
| `POST /fetch` | URLs → their text (`format: "html"` for the source, `"links"` for its anchors) |
| `POST /search-and-fetch` | both in one call, fetching only the winners |

Plus `/corpus/search` (query what you've already fetched, no network),
`/health`, `/v2/status`, `/v2/capabilities` and `/stats`.

There is deliberately **no `/crawl`**. Rendering is a strategy for obtaining a
page, not something a caller wants for its own sake — the ladder escalates by
itself, and a caller choosing the renderer by hand would spend four seconds of
Chromium on a page `direct` serves in two hundred milliseconds. Where forcing
it is genuinely useful, that's the `render` parameter — `always` puts the
renderer first, `never` keeps to the cheap tiers.

`/extract` and `/search-and-extract` remain as aliases. So does
`/extract-with-links`, which is `/fetch` with `format: "links"`: the anchors kept
and rendered as markdown, resolved to absolute URLs. For link discovery rather than
reading — an index page's value is what it points at, and the article
extractors drop that by design.

## The fetch ladder

Three tiers, tried in order, and **a tier only succeeds if it yields readable
prose**. That's the whole design, and it comes from a measurement:

```
page          direct   tls    crawl4ai
quotes-js          0     0       ✓        ← JavaScript-only page
indeed             0  2340       ✓        ← 403s an ordinary TLS handshake
wikipedia      26861 26861       ✓
```

`quotes-js` returns **HTTP 200, a complete response body, and zero characters
of article text.** A ladder that escalates on failed fetches declares victory
there and hands you an empty shell. So escalation is driven by recovered text,
and anything under 600 characters counts as a miss.

| tier | speed | what it's for | needs |
| --- | --- | --- | --- |
| `direct` | ~2.1s | most pages. requests + trafilatura | nothing |
| `tls` | ~0.3s | a real Chrome TLS fingerprint, no browser | a library |
| `crawl4ai` | ~4.4s | renders JavaScript, locally | a container you run |

The renderer solves **JavaScript, not anti-bot**. Some sites answer with a
managed challenge — an interactive "verify you are human" checkbox — and that
is not a fingerprinting problem to be tuned away. Measured: a real Chrome, on
the same machine and address, received the *identical* 403 those sites give us,
carrying the same challenge document. It could run the challenge; it still
ended at a checkbox waiting for a person.

dethrottled reports that honestly as `challenge_needs_a_human` rather than
`http_403`, because "forbidden" and "willing, if you tick a box" are different
facts and only one of them means stop asking.

Only `direct` is required. `tls` is **faster than plain requests** (310ms vs
536ms median) because curl-impersonate is C — it's not a slow fallback, it's a
quick one. A shorter ladder solves fewer pages and nothing breaks.

There is deliberately **no relay tier**. Every option needs an account (a
Cloudflare Worker), a third party who then learns every URL you fetch (a hosted
reader, a CORS proxy), or an address range that anti-bot vendors blocklist on
sight (Tor publishes its exit list in real time).

## Everything it can read

Content type is decided from the **file signature**, never the server's header —
an HTML error page served as `application/vnd.ms-excel` is routine, and handing
that to a spreadsheet parser produces either a crash or, worse, nonsense that
looks like data.

| kind | formats |
| --- | --- |
| web | HTML, via trafilatura → resiliparse → selectolax |
| documents | PDF (+ OCR for scans), DOCX, XLSX, XLS, PPTX, CSV/TSV |
| open formats | ODT, ODS, ODP, EPUB, RTF |
| video | YouTube captions, from the URL |

Routing costs **0.6µs** for the common case. MarkItDown does this same job with
a neural network; this is forty lines of byte comparison.

## Ranking

Results arrive from three sources in no order at all. Up to three stages fix
that, each optional:

1. **BM25** — lexical, no model, microseconds
2. **Corpus merge** — passages you've already fetched, competing with the web
3. **Cross-encoder** — a real model, over a shortlist of 40 only

Measured on a pool where one document is right and the rest are plausible
neighbours: **MRR 0.646 → 0.833**. It costs 16ms and earns it.

Two orderings are deliberate: **rank before fetching**, because fetching is the
expensive step; and **merge the corpus before ranking**, so the reranker judges
web and local results on equal terms.

## The corpus

Every page fetched is split into passages, embedded and stored in SQLite, so
the cache quietly becomes searchable:

```bash
curl -G localhost:8787/corpus/search --data-urlencode 'q=how are documents ranked'
```

No vector database. Brute-force cosine over 50,000 passages takes **2.5ms** —
an index would be machinery with nothing to do.

## It learns which sites are readable

Some domains reliably return nothing: single-page apps that render from a
private API, syndication aggregators, consent walls. Ranking answers *"is this
relevant?"* and has no idea whether a page can be **read**.

So dethrottled records per-domain extraction outcomes and uses them at exactly
one point — choosing which of the already-ranked results to spend a fetch on.
It never reorders by relevance and never drops a result.

Crucially it's **measured, not declared**. No blocklist ships. Evidence decays
with a 14-day half-life, so a site that starts working recovers on its own.

## Install

```bash
pip install 'dethrottled[all]'           # everything below. Start here
```

The base install is deliberately small — eight pure-Python dependencies — and
gives you search, the `direct` fetch tier and the extraction cascade. Every
other capability is an extra, because the heavy pieces are the ones many
callers never use:

```bash
pip install dethrottled                  # search, direct fetch, extraction
pip install 'dethrottled[documents]'     # + PDF, Office, ODF, EPUB, RTF, OCR
pip install 'dethrottled[tls]'           # + the TLS tier
pip install 'dethrottled[semantic]'      # + the corpus
pip install 'dethrottled[rerank]'        # + the cross-encoder
pip install 'dethrottled[media]'         # + video transcripts
```

Every optional import is guarded: a missing extra removes a capability, it does
not break one, and `/v2/capabilities` reports exactly what this install can
actually do rather than what the code is capable of.

Runs on **x86-64 and ARM64**, Linux, macOS and Windows, Python 3.10–3.13. CI
tests both architectures. Nothing needs a compiler — **including on a Pi**.

One model, 87MB:

```bash
./scripts/fetch-models.sh
```

## Politeness

This fetches other people's pages, so: `robots.txt` honoured at every tier and
cached; one request per domain at a time with a 1.5s floor; an honest,
contactable User-Agent; bounded retries; a 10MB response ceiling.

That 1.5s floor means **fetching many URLs from one site is slow by design** —
8 URLs from one domain take 12.7s, 8 URLs from eight domains take 3.3s. That's
the politeness working, not a defect.

## Licensing

MIT, and it ships no weights. Everything it downloads is permissive:
all-MiniLM-L6-v2 (Apache-2.0) and ms-marco-MiniLM-L-12-v2 (Apache-2.0).

There is deliberately **no non-commercially-licensed component in any code
path** — "optional" is not something a licence audit can rely on.

## Documentation

- [**TLDREADME.md**](TLDREADME.md) — everything, in depth. Every tier, every
  measurement, every rejected alternative
- [USAGE.md](USAGE.md) — the API and every configuration knob
- [ARCHITECTURE.md](ARCHITECTURE.md) — how the pieces fit
- [SECURITY.md](SECURITY.md) — **read before exposing this to a network**

## Licence

MIT. See [LICENSE](LICENSE).
