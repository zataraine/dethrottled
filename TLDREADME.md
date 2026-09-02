# TLDREADME

*Too long; **do** read.*

Everything dethrottled does, why each piece is shaped the way it is, and the
measurement behind every decision. [README.md](README.md) is the short version.

Almost nothing here was decided by preference. Where a number appears, it came
from a script in `scripts/` that you can run yourself.

---

## Contents

1. [The thesis](#1-the-thesis)
2. [The request path](#2-the-request-path)
3. [Search](#3-search)
4. [Fetch: the tier ladder](#4-fetch-the-tier-ladder)
5. [Budgets, politeness and cooldown](#5-budgets-politeness-and-cooldown)
6. [Content routing](#6-content-routing)
7. [Extraction](#7-extraction)
8. [Documents](#8-documents)
9. [OCR](#9-ocr)
10. [Video](#10-video)
11. [Ranking](#11-ranking)
12. [The corpus](#12-the-corpus)
13. [Domain fetchability](#13-domain-fetchability)
14. [Caching](#14-caching)
15. [Health](#15-health)
16. [Concurrency](#16-concurrency)
17. [Configuration reference](#17-configuration-reference)
18. [What was rejected, and why](#18-what-was-rejected-and-why)
19. [Known limitations](#19-known-limitations)
20. [Reproducing every number](#20-reproducing-every-number)

---

## 1. The thesis

**A tier succeeds only if it yields readable prose.**

That is the single idea the whole project is built on, and it comes from a
measurement rather than an intuition. Consider a JavaScript-only page:

```
                     bytes returned    article text
direct fetch                  5,120               0
```

HTTP 200. A complete, valid response body. Not one word of article in it.

A pipeline that escalates on *failed fetches* stops there and reports success.
Everything downstream — ranking, embedding, whatever reads the output — then
works on nothing. So escalation here is driven by **recovered text**, and a
result under `THIN_CHARS` (600) is treated as a miss even when the fetch was a
textbook success.

Everything else in this document follows from that.

---

## 2. The request path

```
query
  ├─ web search (duckduckgo, bing)        ┐
  ├─ Bing News RSS                        ├─ pooled, deduplicated on
  ├─ SearXNG            (optional)        │  canonical URL and on title
  └─ Google News RSS                      ┘
        │
        ├─ + corpus passages the web did not return
        │
        ├─ BM25 over title + first 240 characters
        ├─ cross-encoder reranks the top 40
        ├─ known-unreadable domains moved to the back of the fetch queue
        │
        └─ fetch only the winners
              ├─ is it a video URL?  → captions, no network spent on the page
              └─ otherwise the ladder:
                    direct → tls → crawl4ai
                    budget checked BETWEEN tiers, never during one
                    │
                    ├─ signature says PDF?       → pymupdf → OCR if no text layer
                    ├─ signature says document?  → the right parser
                    └─ otherwise HTML            → trafilatura → resiliparse → selectolax
                          │
                          └─ cached 21 days, and indexed into the corpus
```

Three orderings are deliberate.

**Rank before fetching.** Fetching costs seconds per page; ranking costs
microseconds, and does not need page bodies. A pipeline that fetches then ranks
has already paid for every page it is about to discard.

**Merge the corpus before ranking.** A corpus passage and a web result are both
candidate answers. Appending the corpus to the end of a ranked list silently
declares every local hit worse than every web hit.

**Check the video case before the ladder.** No tier extracts prose from a video
player, so spending three of them proving it is waste.

---

## 3. Search

Four sources, all keyless. None requires an account.

### The sources

| source | what it is | configured by |
| --- | --- | --- |
| `web-duckduckgo` | general web, via `ddgs` | on by default |
| `web-bing` | general web, via `ddgs` | on by default |
| `bing-news-rss` | news, real publisher URLs | always on |
| `google-news-rss` | headline discovery only | always on |
| `searxng` | your own instance, dozens of engines | `DETHROTTLED_SEARXNG_URL` |

### Which engines actually work

`ddgs` bundles scrapers for a dozen engines. Most of them do not work. Measured
over 12 queries from one residential connection:

```
engine       results  median ms  unique domains  answered  failures
bing              72       3057              20      9/12         3
google            48        325              24      6/12         6
duckduckgo        32         96              13      4/12         8
brave             32        246               6      4/12         8
startpage         16       1359               7      2/12        10
mojeek             0          -               0      0/12        12
yahoo              0          -               0      0/12        12
wikipedia          0          -               0      0/12        12
RSS (built-in)    33       1094              25      6/12         0
```

Three engines failed **every** query, and still failed with a four-second gap
between requests — those are hard blocks, not rate limits, and no amount of
politeness recovers them. They are not in the default list.

Two things are worth noticing. `duckduckgo` at 96ms is by a distance the
fastest source in the stack. And the built-in RSS sources have the **highest
unique-domain count and zero failures** — they are the reliable floor, which is
why the scraped engines were added alongside them rather than instead of them.

### Why RSS at all

**Bing News RSS** is the primary news source because its links are
`apiclick.aspx` redirectors carrying the destination in a `url=` parameter, so
the real publisher URL is recoverable with **no extra request**. That property
matters more than raw result count.

**Google News RSS** is headline discovery only. Since the 2024 change its links
are JavaScript redirect shells that resolve back to `news.google.com` and are
robots-disallowed; base64 decoding no longer recovers the target. Headlines it
surfaces are resolved to real URLs through a bounded, cached lookup and dropped
if that fails.

### Engine health and resting

Free engines block self-hosted callers as a matter of routine, and SearXNG
suspends an engine on CAPTCHA. So:

- the engine list is **broad on purpose** — suspensions degrade instead of
  breaking, and heal on their own. A narrow list turns one CAPTCHA into an
  outage
- an engine that refuses is **rested for 30 minutes**, then tried again. A
  block costs one request per engine per half hour rather than one per search
- **the last engine standing is never rested.** A wrong health record must not
  be able to turn every search into an empty result

The record is persisted, because the process that learns an engine is blocked
is usually not the one that pays for asking it next. It is written under a lock
and renamed into place atomically — see [Concurrency](#16-concurrency) for what
happened before it was.

### Deduplication

One story is one row, however many sites carried it.

- **canonical URL** — tracking parameters, fragments, `m.`/`amp.` hosts and
  trailing slashes are stripped, so one article is not three rows
- **title** — a live run returned the same wire piece from three publishers.
  The title key has a length floor so that "Home" and "News" do not collapse
  unrelated sites

### The reputable sweep

Optional, and driven entirely by a list the **caller** supplies. When a pool
comes back thin on trusted publishers it re-asks a rotating handful of them
directly with `site:` scoping.

It exists because of a measured failure. Free sources find the right stories
but surface them through syndication, and **of eight blocked syndicator pages,
zero declared a canonical URL pointing at the original.** They self-canonicalise
and present the content as their own, so there is nothing to follow — the pool
has to change rather than the labelling.

No default domain list ships. A search service that decides for you what counts
as a real publisher is one you cannot use for a subject it was not built for.

---

## 4. Fetch: the tier ladder

Three tiers. Only the first is required.

### `direct` — ~2.1s, needs nothing

`requests` with an honest User-Agent, robots.txt honoured, a 10MB ceiling.
Solves most pages. This is also where content routing happens (§6).

The 10MB ceiling is not the obvious 3MB, and the reason is instructive: **a
truncated HTML download is worse than a failed one.** The parser still
succeeds, on a document missing its tail, and reports a short article rather
than an error. Real pages get close to the old ceiling — one measured at
2.34MB — so it was about to start silently shortening articles.

### `tls` — ~0.3s, needs a library

A real Chrome TLS fingerprint, via `curl_cffi`, with no browser.

A great deal of what gets called bot detection never looks at your User-Agent
at all. It looks at the **shape of the TLS handshake** — cipher order,
extension order, ALPN, the JA3/JA4 hash — and every Python HTTP client has a
conspicuously non-browser one.

This tier is **faster than plain requests** — 310ms median against 536ms —
because curl-impersonate is C. It is a quick fallback, not a slow one, which is
why it sits second.

Profile choice is not cosmetic. Measured across seven fingerprints:

```
host            chrome  chrome131  chrome124  safari17  safari15  edge101  firefox133
indeed         200/418k   200/418k   200/418k  200/418k  200/418k      403    200/418k
reddit         200/163k   200/163k   200/163k    200/8k    200/8k   200/8k      200/8k
stackoverflow       403        403        403       403       403      403         403
```

Indeed passes on Chrome, Safari and Firefox and **403s on Edge**. Reddit yields
163KB on the Chrome family and 8KB on everything else. And StackOverflow
refuses every fingerprint — that is IP reputation, which no client-side trick
touches.

### `crawl4ai` — ~4.4s, needs a container

Headless Chromium you host. The only local answer to a page whose content does
not exist until JavaScript has run.

It speaks two dialects and probes once to find out which:

- `POST /crawl` — upstream Crawl4AI's Docker server, port 11235 (the default)
- `POST /render {url, timeout_ms}` — a hand-rolled worker

Two dialects because both are reasonable things to point at, and guessing wrong
costs a **silently dead tier** rather than a loud error. `auto` tries `/crawl`,
falls back to `/render` on a 404, and remembers which answered.

Crawl4AI already extracts, and its extraction beats re-extracting from its
HTML: measured on a JavaScript-heavy article, its cleaned HTML was 6,586
characters that trafilatura could not read at all, while its markdown held
2,339 characters of clean prose. So markdown wins when there is enough of it.

`examples/crawl4ai_server.py` is a forty-line server for people who install
Crawl4AI from pip, which ships the crawler but not the HTTP service.

### `jina-reader` — optional, off by default

`r.jina.ai`, unauthenticated. It works, and it is the only free thing that
reads certain syndication aggregators.

It is off by default because **every URL you send it is a URL you have told
somebody else you were interested in**, and that is a real cost even when the
money is zero. Enable with `DETHROTTLED_ENABLE_JINA=1`.

It is also inconsistent: measured on the same page in consecutive runs, 2,777
characters and then 422. It is rate-limited rather than metered, and it
degrades quietly under load.

### Why there is no relay tier

A relay — fetching from a different egress IP — would solve the remaining hard
cases. Every option was evaluated and every one fails a requirement:

| option | why not |
| --- | --- |
| Cloudflare Worker | needs an account. Disqualifying for a no-account tool |
| Tor | the exit list is published in real time and pre-blocklisted by the exact anti-bot vendors these sites use |
| public CORS proxies | a third party with worse reliability than the reader we already made optional |
| Wayback Machine | tested: **no capture for any of the pages that fail**. It only helps for content that already works |

So there is none. The gap is documented in [Known limitations](#19-known-limitations)
rather than papered over.

---

## 5. Budgets, politeness and cooldown

### Time budgets

Checked **between** tiers, never during one. A tier already running is allowed
to finish, because killing a fetch that is about to succeed wastes everything
spent on it. This stops the *next* attempt, which is where a runaway page
actually costs its minute.

| budget | default | applies to |
| --- | --- | --- |
| `PAGE_BUDGET` | 60s | a URL somebody asked for by name |
| `PAGE_BUDGET_BULK` | 25s | each page in a bulk fetch |

The bulk budget is smaller because it multiplies: six results at 60s each is
six minutes for one search.

The `tls` tier has its own 8-second timeout, and that is not a detail — it is
the reason the tier is safe to add. At the standard 20s it ate the 25s bulk
budget and **starved the renderer**: crawl4ai fell from 6 solves to 4, outright
failures rose from 7 to 16, and tls itself solved 2. A tier that occasionally
helps must never be able to spend the budget belonging to the tier that usually
helps.

### Volume budgets

Hourly, per tier. These used to be lifetime counters that nothing reset, so the
renderer silently stopped rendering after 8 pages and the reader after 25 — for
the rest of the process, invisibly. `/stats` surfaces what has been spent.

### Politeness

- **robots.txt honoured at every tier**, and cached 24 hours. A relay is a
  different route to the same publisher, not permission to ignore what they
  asked for
- **one request per domain at a time, with a 1.5s floor**
- an honest, contactable User-Agent, overridable but say what you are
- bounded retries, a 10MB response ceiling

The domain gap has a real consequence: **8 URLs from one domain take 12.7s; 8
URLs from eight domains take 3.3s.** Concurrency does not help within a domain,
by design.

### Tier cooldown

A rate limit is a temporary condition wearing the costume of a permanent one.
Asking a tier that just said 429 to try the next page wastes a timeout and
deepens the limit; dropping it permanently throws away a tier that will be fine
in ten minutes.

So a tier that refuses is **rested**: skipped for a cooling period, then tried
again. Backoff doubles per consecutive refusal, capped at an hour, and **clears
completely on success** so a brief wobble costs one cooldown rather than a
permanent handicap.

**Only shared-service tiers can rest** — `crawl4ai`, `jina-reader`. This
distinction is the entire safety of the mechanism. `direct` and `tls` are not
services, they are "make an HTTP request", per host. When the cooldown was
applied to them, one 403 from one site rested the tier globally: direct fell
from 42 solves to 7, the renderer was handed 30 pages it had no business
rendering, and end-to-end extraction fell **87% → 67%**. A 403 from one host
says nothing about the next.

What counts as a refusal is deliberately narrow: 401, 403, 407, 418, 429, 503
and transport-level give-ups. A 404 is about the page, not the tier.

---

## 6. Content routing

Two decisions, both on the hot path, neither using a model.

### Is this a video? — from the URL, before any network

A host allowlist plus an eleven-character ID pattern. Matching loosely would
turn every youtube.com link — a channel, a search, the home page — into a
transcript lookup that could only fail.

**0.9–2.9µs per URL.**

### What are these bytes? — from the signature, not the header

Servers mislabel constantly. An HTML error page served as
`application/vnd.ms-excel` is routine, and handing that to a spreadsheet parser
produces either a crash or, worse, nonsense that looks like data. So the bytes
decide:

| signature | meaning |
| --- | --- |
| `%PDF` | PDF → pymupdf, then OCR if there is no text layer |
| `PK\x03\x04` | a zip → read the directory to see *which* zip |
| `\xd0\xcf\x11\xe0…` | OLE2 legacy Office |
| `{\rtf` | RTF |
| *(none)* | CSV, but only when a header or extension says so |

OOXML, OpenDocument and EPUB are **all zips** sharing the same four magic
bytes, so the signature says "a zip" and nothing more. What distinguishes them
is what is inside — a fixed member path for Office (`xl/workbook.xml`,
`word/document.xml`, `ppt/presentation.xml`), a `mimetype` member for the other
two, which both specifications require to be first and uncompressed precisely
so it can be read like this.

Measured cost:

```
sample        bytes    µs/call
html page      4875        0.6      ← the common case
pdf           40015        0.6
rtf              41        0.6
xlsx            257       12.5
odt             258       23.0
epub            239       26.9
csv (hinted)  14800        1.8
```

**0.6µs for HTML** — the signature check bails on the first comparison, so
adding five formats cost nothing on the common path.

CSV is the one format with no signature, and it is detected only when the
content type or extension says so. Guessing would make every HTML page a
one-column CSV.

---

## 7. Extraction

Three extractors, tried in order. All ship aarch64 wheels — this runs on a Pi
without a compiler.

The order was set by measuring five candidates on the same ten real pages,
scoring what they recovered **and** how much of it was page furniture:

```
extractor      median ms   junk %   note
resiliparse          2.0     5.5%   fastest; misses main content entirely on
                                    some layouts (78 chars where trafilatura
                                    read 7,445)
selectolax           2.6     3.1%   fast, and never returns nothing
readability         27.4     1.5%   low junk because it often returns almost
                                    nothing: 0 chars on one page, 239 on another
trafilatura         33.4     0.9%   cleanest output, best coverage
BeautifulSoup       37.2    66.5%   slowest AND two thirds boilerplate
```

"Junk" is the fraction of lines matching page furniture — cookie banners, share
buttons, subscribe prompts, copyright lines.

**trafilatura** leads on quality. **resiliparse** is second because it is
sixteen times faster and catches real trafilatura misses — on one news front
page it recovered 13,955 characters where trafilatura found 3,545.
**selectolax** is the floor: not a content extractor at all, just a very fast
parser with the furniture removed, which is exactly what a last resort should
be.

**readability and BeautifulSoup were both dropped.** BeautifulSoup was the
worst rung on both axes at once — slowest *and* dirtiest — and readability
rarely earned its 27ms. That also removed a dependency.

More text is not better text. The easiest way to win a characters-recovered
metric is to return the navigation menu.

Whatever produced the text is recorded in the `tier` field as
`fetch-tier/extractor`, so a site that only ever falls through to the crude
rung is visible rather than silently producing mush.

---

## 8. Documents

Every parser is a direct call. See §18 for why MarkItDown is not used.

| format | library | notes |
| --- | --- | --- |
| PDF | pymupdf | 25MB budget, downloaded whole or not at all |
| XLSX | openpyxl | rows keep their line breaks |
| XLS | xlrd | legacy binary spreadsheets |
| DOCX | python-docx | |
| PPTX | python-pptx | titles and body placeholders |
| ODT/ODS/ODP | odfpy | what most European public bodies publish in |
| EPUB | ebooklib | a zip of XHTML, read with the web extractor |
| RTF | striprtf | pure Python, validates its own signature |
| CSV/TSV | stdlib | |

`.doc` and `.ppt` (OLE2) are **refused by name**, not silently. Reading them
needs a ~500MB converter; naming the gap keeps it visible if it ever starts
mattering.

### Why rows keep their line breaks

Structured text does not go through the prose tidier. In a table the row
boundary carries meaning: run two rows together and
`Denmark | 2024 | 5120 Denmark | 2023 | 4560` no longer says which number
belongs to which year. Spreadsheets, OCR pages and slide decks all keep theirs.

### Bounds

`MAX_ROWS` (2000) and `MAX_CELL` (200) exist because a national statistics
export can run to six figures of rows, nothing downstream reads that far, and
one pathological cell must not become the whole extraction.

### The PDF size budget

PDFs get a **larger, separate** budget (25MB) and are downloaded whole or not
at all, because **a truncated PDF is not a short PDF**. The cross-reference
table lives at the *end* of the file, so cutting the stream mid-download yields
bytes no parser can open — an 8.3MB statistics yearbook failed as "no text
layer" for exactly that reason.

---

## 9. OCR

A PDF with no text layer is otherwise a dead end, and the documents most likely
to be scans are the ones least likely to exist anywhere else: government
tenders, municipal filings, older statistical yearbooks. The web copy *is* the
scan.

Pages are rendered by pymupdf and piped to Tesseract as PNG **on stdin**, so
nothing touches disk.

### 150 dpi, not 220

```
150dpi   render 0.09s   ocr 1.44s
220dpi   render 0.14s   ocr 1.75s
```

150 wins twice over — faster, **and cleaner**, because at 220 Tesseract began
reading a sideways watermark as text.

### One language, not several

The obvious approach is to pass every installed language and let Tesseract sort
it out. Measured against a French page with known ground truth, that is the
worst option available:

```
-l fra           similarity 0.997   0.32s
-l eng           similarity 0.981   0.33s
-l eng+fra       similarity 0.986   0.46s
-l eng+fra+ara   similarity 0.986   0.50s
-l ara           similarity 0.092   0.31s
```

Stacking languages makes Tesseract hedge: slower **and** less accurate than the
right single pack. And Arabic data on Latin text is catastrophic, so it can
never be included as insurance.

So the language is **detected once per document** and one pack is used. Script
comes from Tesseract's own OSD. Telling French from English is done on the
first page's English output — `eng` reads French at 0.981, more than good
enough to recognise *which* language it is even though it transcribes it worse.

### Detecting real prose from confident nonsense

The first version of the Latin check asked whether the probe found *few* words,
which was wrong in an instructive way. English data pointed at an Arabic page
does not return little text — it returns **plenty of confident nonsense**
("lub Yl glaxule juaddl"), so the word count stayed high and a genuinely Arabic
document was read as English at a similarity of 0.007.

Real prose is thick with function words; noise has almost none. That **ratio**
separates them where a raw count does not.

### Bounds

8 pages per document, 30s per page. OCR costs ~1.4s a page, so it runs for a
URL somebody asked for by name and **not** across a page of search results.

---

## 10. Video

A video page is the one case the ladder cannot win. Fetching returns a player,
rendering returns a player, and no extractor gets prose from either. The words
are in the caption track, served separately and free to read.

So a video URL is recognised **before any network is spent** and routed to its
captions. Recognised shapes: `/watch?v=`, `youtu.be/`, `/embed/`, `/shorts/`,
`/live/`, `/v/`.

Captions arrive as timed fragments, often mid-sentence, and are joined into
continuous prose — every consumer of this wants sentences, not a subtitle file.
Bracketed cues (`[Music]`, `[Applause]`) are dropped: they describe sounds, not
speech.

### The honest caveat

`youtube-transcript-api` talks to YouTube's own internal endpoint with no key
and no account. **YouTube blocks datacentre address ranges aggressively**, so
this works from a machine you own and frequently will not from a cloud host.

That is reported rather than hidden. A block comes back as
`transcript_blocked_from_this_address`, distinct from `no_transcript_available`
and `video_unavailable`, because those call for completely different responses
from whoever reads the logs.

### What it deliberately does not do

It does not transcribe audio. Speech-to-text means a model, a few hundred
megabytes and minutes of CPU per video — a different product. A video with no
captions is reported as having none.

---

## 11. Ranking

Three stages, each optional, each degrading to the one before.

### BM25

Chosen on measurement, not fashion. Against a neural bi-encoder on the pool
this was built for, **BM25 placed more useful rows in the top 14 and took no
time at all rather than 167 seconds.** Rare words are what discriminate between
search results, and a bi-encoder trained for semantic similarity prefers
documents that are broadly on-topic to documents that contain the answer.

It scores on **title plus the first 240 characters**, not the whole page.
Measured: ranking on 3,000 characters of body and on title-plus-240 both put 5
of 10 useful rows in the top 14, but the short form put the best one at **rank
1 rather than rank 3**. The rest of a page is navigation, cookie notice and
footer, and all of it votes.

### Recency

Multiplicative and bounded, never additive. Freshness may reorder results that
already earned a relevance score, and may **never promote an irrelevant one**.
Undated rows count as neutral rather than old, because the single most useful
result in the case this was built for was undated.

### Cross-encoder

Reads query and document together with attention across both, which is why it
beats a bag of words and why it costs per document. It therefore **never sees
the whole pool** — reranking all 189 rows of a real pool cost eleven times as
much as reranking the top forty, for the same answer.

Does it earn its place? Measured on pools where one document is right and the
rest are plausible neighbours — same vocabulary, same subject, wrong document:

```
query                                          bm25    +rerank
how does BM25 handle document length           1.00       1.00
why does a headless browser get detected       0.25       1.00   ← 4th to 1st
what makes a TLS fingerprint identifiable      0.33       0.33
how do I stop one slow page costing a run      1.00       1.00
                            MEAN RECIPROCAL   0.646      0.833
                            cost per query   0.07ms     16.4ms
```

**+0.188 MRR for 16ms.** It earns it.

It is English-only, for two separate reasons. Licensing: the obvious
multilingual cross-encoder is CC-BY-NC-4.0, and a non-commercial component has
no place in an MIT repository even as an optional one. Weight: this is an
English-first tool and a second model is half a gigabyte.

BM25 is language-agnostic, so a non-English pool is still ordered. It is the
second stage, and only that, which is English-only.

Every response reports **which stages actually ran** — asking for a reranker
you have not installed gets you lexical ordering, and you should see that
rather than infer it from disappointing results.

---

## 12. The corpus

Every page fetched is split into overlapping passages, embedded, and stored in
SQLite. The cache quietly becomes a searchable corpus.

Passages **overlap** because the sentence answering a question has no
obligation to sit tidily inside one window — a fact split across a boundary is
a fact neither passage can be retrieved for. The **title rides on every
passage**, since a paragraph three screens into an article rarely names its own
subject.

The corpus indexes the first **4,000 characters** of a page, deliberately less
than the 8,000 `/fetch` returns. Two different jobs: a caller wants the whole
article, the index wants the part of it that is *about* something. A page's
tail is footer, comments and related-articles, and indexing those makes them
retrievable — the corpus would start answering questions with boilerplate.

### One model

all-MiniLM-L6-v2. 87MB, 384 dimensions, ~0.3ms per passage.

multilingual-e5-small was carried alongside it and dropped. Measured over ten
documents on neighbouring subjects with ten known-answer questions:

```
model     acc@1    MRR    score margin    size    query
minilm     1.00   1.000          0.242    87MB    1.4ms
e5         1.00   1.000          0.037   465MB    3.3ms
```

Identical ranking. But the **margin** — the gap between the right answer and
the best wrong one — is what decides whether a relevance floor can be set
safely at all, and MiniLM separates by 0.242 where e5 separates by 0.037. Equal
accuracy, six times the headroom, a fifth of the size.

What is given up is cross-language retrieval. The `model` column stays in the
schema, so adding a second index later needs no migration.

### The relevance floor

Cosine similarity **always returns a best match**. Asked something the corpus
has nothing for, it will still rank something first. Returning nothing is the
correct answer to "we have nothing", and much better than the least-bad thing
on file.

Measured on general content:

```
              real answers    nothing-to-answer
minilm       0.375 - 0.585        0.030 - 0.032
```

An enormous empty band, so the floor sits at **0.22** in the middle of it.

The inherited value was 0.40, tuned on one domain-specific corpus, and it cut
straight through the real answers — it was rejecting **half** of them.

### No vector database

Brute-force cosine, measured:

```
passages   matrix RAM   disk   rebuild   search
  20,000        31 MB   42 MB     99ms    1.0ms
  50,000        77 MB  105 MB    236ms    2.5ms
 100,000       154 MB  210 MB    504ms    8.4ms
 200,000       307 MB  419 MB   1009ms   13.8ms
```

Search is a non-issue — 14ms at 200,000 passages. An index would be machinery
with nothing to do.

The **cap is really a memory budget**. 200,000 passages asks for 307MB resident
before Python, the ONNX runtime and the model have taken their share — most of
a 2GB Pi. The default is **50,000** (about 12,000 pages, 77MB), and nothing
breaks at a larger number if you have the memory.

### The matrix is appended, not rebuilt

Dropping the cached matrix on every write meant the next search re-read every
row and rebuilt the whole array — a second of work at 200,000 passages, paid
after every write. And the corpus is written on *every fetch*, so in practice
almost every search paid it.

New rows are now appended to the cached array. Verified byte-identical to a
full rebuild, including under concurrent writers, because a vector drifting out
of step with its metadata returns the right score attached to the wrong
document — which looks like working software.

Pruning still drops the cache: deletion takes rows out of the middle of the
array, and the bookkeeping to patch that is worse than the rebuild.

### Retention

180 days, 50,000 passages, whichever comes first, pruned hourly. Retention only
means something if it runs — `prune()` once had no callers at all.

---

## 13. Domain fetchability

Ranking answers *"is this relevant?"* using title and snippet. It has **no idea
whether the page can be read**. Those are separate questions, and a search
stack that only answers the first will confidently hand you a perfectly
relevant result that yields nothing.

Some domains reliably return nothing: single-page apps rendering from a private
API, syndication aggregators, sites serving a consent wall to everyone.
Measured, one such domain accounted for **every** extraction failure in a
40-page run — nine relevant results, nine empty bodies, nine wasted fetches.

### Measured, not declared

The obvious fix is a blocklist, and it is the wrong fix. A shipped list of
"bad" domains is one person's editorial opinion baked into everyone's install:
wrong for subjects it was not written for, stale silently, and unable to know
that a site fixed itself last month.

So **nothing is declared**. dethrottled records what happened — attempts and
successes, per domain, per deployment — and lets the record speak.

- Laplace-smoothed, so an unseen domain scores **0.5, not 0**. An unseen site
  is unseen, not suspicious
- needs **5 attempts** before it acts. A single failure is a bad afternoon
- acts below a **0.2** success rate — four failures in five
- evidence **decays with a 14-day half-life**, so a site broken in March is not
  condemned in June, and a domain that starts working recovers on its own

### Where it is allowed to act

Deliberately narrow. It is consulted at **one point only**: after ranking, when
choosing which of the already-ranked results to spend a fetch on. The pool is
over-fetched precisely so there is something to choose from.

It **never** reorders by relevance, **never** drops a result, and **never**
touches the relative order of domains it has no complaint about. If the whole
pool is poor domains, they are still what comes back.

`/stats` shows everything it has decided and on what evidence — a system that
quietly deprioritises part of the web should be able to say so.

---

## 14. Caching

SQLite. One lock, commits inside it.

| kind | TTL |
| --- | --- |
| search results | 6 hours |
| page bodies | 21 days |
| robots.txt | 24 hours |

### Keyed on the URL alone

Extract entries are keyed on the URL and **not** on `max_chars`. Keying on the
cap meant every change to a caller's character limit orphaned the entire cache
— thousands of real fetches thrown away by a one-line config edit. Entries are
stored at full length and clipped on read.

### The source is deliberately not cached

`raw: true` returns the page source and **bypasses the cache**, because the
cache stores extracted text and not HTML. Measured across six real pages:

```
page              html     text    ratio
wikipedia      144,767    6,152     23x
bbc            371,249    7,347     50x
cloudflare     210,337    1,617    130x
github         316,387    1,734    182x
                                avg 47x
```

At a realistic 1,659 cached pages that is **6.6MB of text against 317MB with
source**. On a Pi with an SD card, storing source would trade
many-pages-cheaply for few-pages-expensively.

All cached state is disposable. Delete any of it and the system refills it.

---

## 15. Health

`python -m dethrottled.health [--json]`

Written after two failures a conventional health check could not see:

- a render service returned `{"status":"ok"}` for **three and a half days**
  while every render silently stalled behind an exhausted semaphore. The kernel
  had throttled it into memory reclaim 7,392 times; it never OOM-killed, so
  nothing restarted it and nothing logged an error
- a hosted reader reported a routine `daily_cap` for three days while running
  on a **revoked key** — the same message for "come back tomorrow" and for "you
  will never work again"

Both were invisible because the checks asked *are you up?* instead of *can you
do the job?*.

So every probe does real work and inspects the result. Four states:

| state | meaning |
| --- | --- |
| `OK` | did the job |
| `THIN` | worked, returned less than `THIN_CHARS` |
| `DEGRADED` | **answered, and returned nothing usable** |
| `DEAD` | did not answer |
| `OFF` | not configured — has not failed |

`DEGRADED` is the state that hid both faults above: HTTP 200, a response body,
not one word of article text.

`OFF` is the second important one. A tier nobody configured has not failed, and
reporting it as `DEAD` would mean a healthy `pip install` **fails its own
health check** — and a check that cries wolf is one people learn to ignore.

Exit codes: `0` all configured components working, `1` degraded, `2` critical
(every search source dead, or no fetch tier can retrieve a page).

The same principle runs through the API. An unconfigured source reports
`not_configured`, never `down`. "Down" sends you looking for a broken service;
only one of those is where the problem actually is.

---

## 16. Concurrency

FastAPI runs a sync `def` route in a threadpool, so every module-level counter,
cache and file here is touched concurrently in production.

### Do you need async everywhere? No.

The threadpool is not the bottleneck:

```
clients      ok       p50     p95    wall
      8   24/24     5ms   120ms   0.13s
    100  300/300   12ms    17ms   0.21s
```

100 concurrent clients, no meaningful degradation. The only serialisation is
the deliberate 1.5s per-domain politeness gap, and async would not change that
by a millisecond — it is a rate limit, not a blocking-I/O artefact.

Async would likely make things **worse**: `async def` routes calling `requests`,
`sqlite3` and `onnxruntime` — all blocking — would stall the event loop
outright. Sync routes in a threadpool is the correct design here.

### Two defects this surfaced

Both were found by stress, not by reading:

**Cooldown backoff overflow.** It computed `TIER_COOLDOWN * (2 ** (strikes-1))`
and capped the result *afterwards*. Python integers do not overflow, so
sustained failure built a number with hundreds of digits and raised
`OverflowError` converting it to a float — 256 exceptions in a 1,280-refusal
run, and only ever under heavy failure, which is when the cooldown matters
most. The shift is now capped before the power is computed.

**Engine health zeroing itself.** An unsynchronised read-modify-write over a
file written with truncate-then-write. Under 32 threads recording 1,280
failures the final count was **zero** — readers landing mid-write got invalid
JSON, fell back to an empty dict, and wrote that back over everything. Now
locked end-to-end and written via atomic temp-file rename.

Everything else — cache, budgets, domain health, corpus matrix, throttle — was
already correctly locked and passed unchanged.

---

## 17. Configuration reference

Everything is an environment variable. Nothing is required.

### Server

| variable | default |
| --- | --- |
| `DETHROTTLED_PORT` | `8787` |
| `DETHROTTLED_HOST` | `127.0.0.1` |
| `DETHROTTLED_DATA_DIR` | `~/.cache/dethrottled` |
| `DETHROTTLED_MODEL_DIR` | `~/.cache/dethrottled/models` |

`MODEL_DIR` deliberately does **not** follow `DATA_DIR`: pointing the data
directory at a scratch path — which every test does — would otherwise orphan
the model download.

### Sources

| variable | default | |
| --- | --- | --- |
| `DETHROTTLED_WEB_ENGINES` | `duckduckgo,bing` | the two that answer reliably |
| `DETHROTTLED_SEARXNG_URL` | `""` | empty = tier skipped |
| `DETHROTTLED_SEARXNG_ENGINES` | broad list | keep it broad |
| `DETHROTTLED_ENGINE_REST_SECONDS` | `1800` | how long a refusing engine rests |
| `DETHROTTLED_PROBE_QUERY` | `technology` | health probe query |

### Fetch tiers

| variable | default | |
| --- | --- | --- |
| `DETHROTTLED_ENABLE_TLS` | `1` | the curl_cffi tier |
| `DETHROTTLED_TLS_IMPERSONATE` | `chrome` | fingerprint profile |
| `DETHROTTLED_TLS_TIMEOUT` | `8` | must stay small — see §5 |
| `DETHROTTLED_CRAWL4AI_URL` | `""` | empty = tier skipped |
| `DETHROTTLED_CRAWL4AI_API` | `auto` | `auto`, `crawl`, `render` |
| `DETHROTTLED_ENABLE_JINA` | `1` pip / `0` compose | **leaves your network** |

### Behaviour

| variable | default | |
| --- | --- | --- |
| `DETHROTTLED_THIN_CHARS` | `600` | below this a result escalates |
| `DETHROTTLED_PAGE_BUDGET` | `60` | seconds, named URL |
| `DETHROTTLED_PAGE_BUDGET_BULK` | `25` | seconds, per page in bulk |
| `DETHROTTLED_MAX_HTML_BYTES` | `10000000` | |
| `DETHROTTLED_TIER_COOLDOWN` | `300` | first rest, doubles |
| `DETHROTTLED_TIER_COOLDOWN_MAX` | `3600` | cap |
| `DETHROTTLED_USER_AGENT` | honest default | say what you are |

### Corpus and ranking

| variable | default | |
| --- | --- | --- |
| `DETHROTTLED_CORPUS_FLOOR` | `0.22` | measured, see §12 |
| `DETHROTTLED_CORPUS_MAX_PASSAGES` | `50000` | a memory budget |
| `DETHROTTLED_CORPUS_RETENTION_DAYS` | `180` | |
| `DETHROTTLED_CORPUS_AUTOINDEX` | `1` | index every fetched page |
| `DETHROTTLED_CORPUS_INDEX_CHARS` | `4000` | less than `/fetch` returns |
| `DETHROTTLED_XENC_SHORTLIST` | `40` | rows the cross-encoder sees |
| `DETHROTTLED_EMBED_THREADS` | `4` | also used by OCR |

### Domain health

| variable | default | |
| --- | --- | --- |
| `DETHROTTLED_DOMAIN_HEALTH` | `1` | |
| `DETHROTTLED_DOMAIN_MIN_ATTEMPTS` | `5` | evidence before acting |
| `DETHROTTLED_DOMAIN_FLOOR` | `0.2` | |
| `DETHROTTLED_DOMAIN_HALF_LIFE` | `14` | days |

### Documents, OCR and video

| variable | default | |
| --- | --- | --- |
| `DETHROTTLED_DOC_MAX_ROWS` | `2000` | |
| `DETHROTTLED_DOC_MAX_CELL` | `200` | |
| `DETHROTTLED_PDF_MAX_BYTES` | `26214400` | whole or not at all |
| `DETHROTTLED_OCR_DPI` | `150` | measured; 220 is worse |
| `DETHROTTLED_OCR_PAGE_CAP` | `8` | |
| `DETHROTTLED_OCR_LANGS` | `eng+fra+ara` | candidates, one is chosen |
| `DETHROTTLED_ENABLE_TRANSCRIPTS` | `1` | |
| `DETHROTTLED_TRANSCRIPT_LANGS` | `en,en-GB,en-US` | preference, not a filter |

---

## 18. What was rejected, and why

### MarkItDown

`markitdown[all]` is **375MB across 52 packages** and pulls in:

- `azure-ai-documentintelligence`, `azure-ai-contentunderstanding`,
  `azure-identity`, `azure-core` — **cloud SDKs requiring accounts**, in a tool
  whose entire claim is that it needs none
- `magika` + `onnxruntime` — **a neural network to identify file types**, a job
  done here in forty lines of byte comparison at 0.6µs
- `pandas`, `SpeechRecognition`, `pydub`

It also *wraps* python-docx, openpyxl and pdfminer — the libraries called
directly here. It would be an abstraction layer over our own dependencies at
four times the entire install.

### BeautifulSoup and readability

Measured worst on both axes (37.2ms, 66.5% junk) and rarely earning its cost,
respectively. See §7.

### multilingual-e5-small

Equal accuracy, six times worse score margin, five times the size. See §12.

### jina-reranker-v2

CC-BY-NC-4.0. No non-commercial component belongs in an MIT repository, even an
optional one.

### Tor, CORS proxies, Cloudflare Workers, Wayback

All four evaluated as relay tiers. All four disqualified. See §4.

### Async everywhere

Measured as unnecessary and probably harmful. See §16.

---

## 19. Known limitations

**Honest list. These are real and none of them is hidden by the code.**

1. **Syndication aggregators and hard paywalls.** Sites that render entirely
   from a private API and refuse automation return nothing. One measured
   aggregator returns 42KB of HTML whose entire visible text is its own brand
   name. No free tier reads it: the renderer is blocked by anti-bot detection,
   and Wayback has no capture. Enabling `jina-reader` recovers some of these.

2. **The renderer is not an anti-bot tool.** Crawl4AI renders JavaScript. It
   does not disguise you, and a site refusing your IP refuses a headless
   browser from that IP too. Measured with `render=always`: loc.gov,
   science.org, congress.gov and stackoverflow.com all returned "blocked by
   anti-bot protection", while encyclopedia.com — nav-heavy but not walled —
   rendered fine. If you came here hoping for a Cloudflare bypass, there
   isn't one, and §4 explains why no relay tier exists either.

3. **IP reputation.** Some hosts refuse an address rather than a client. One
   site returned 0 characters locally and 26,099 from a US egress — no
   client-side technique touches that. If you are outside the US/EU you will
   hit this more.

4. **Geographic variation.** Every measurement in this document was taken from
   a single residential connection in one country. Your results will differ.

5. **Video transcripts need a residential IP.** YouTube blocks datacentre
   ranges. Works from a machine you own, often not from a cloud host.

6. **Fetching many URLs from one domain is slow by design.** 1.5s between
   requests to the same host. 8 URLs from one domain: 12.7s.

7. **English-first.** BM25 and the fetch stack are language-agnostic, but
   reranking is English-only and cross-language corpus retrieval was dropped.

8. **Legacy `.doc` and `.ppt` are not read.** Refused by name rather than
   silently.

9. **Scraped search engines rot.** Three of eight tested were hard-blocked.
   Expect the working set to change; the resting logic handles it, the list may
   need revisiting.

---

## 20. Reproducing every number

Every measurement above comes from a script in this repository.

| script | what it measures |
| --- | --- |
| `scripts/benchmark.py` | end-to-end, 20 awkward cases |
| `scripts/probe_fetchers.py` | fetch strategies head to head |
| `scripts/probe_impersonate.py` | TLS fingerprints, and extractors on the same bytes |
| `scripts/probe_extract.py` | extractor quality and speed |
| `scripts/probe_search.py` | every keyless search engine |
| `scripts/probe_embed.py` | embedding models, accuracy and margin |
| `scripts/probe_rerank.py` | does reranking actually help |
| `scripts/probe_routing.py` | routing correctness and cost |
| `scripts/probe_wayback.py` | the archive as a relay substitute |
| `scripts/ab_extract.py` | fixed-URL A/B between two servers |
| `scripts/ab_jina.py` | four configurations, end to end |
| `scripts/stress_state.py` | shared state under 32 threads |
| `scripts/stress_server.py` | the server under parallel load |
| `scripts/pick_uncached.py` | build a gate set a server has never seen |

### A note on benchmark methodology

The end-to-end benchmark has **high run-to-run variance**, because the free
search engines return a different URL set every run — 55 results, then 60, then
55. Comparing extraction rates across different pages is not a comparison, and
it produced three contradictory readings before this was understood.

`scripts/ab_extract.py` is the trustworthy gate: one fixed URL list, given to
every server, extraction only.

And **check the cache before believing any comparison**. One early parity run
showed a rival stack winning 40/40 at a median of 2ms per page — it had not
touched the network at all. `scripts/pick_uncached.py` builds a URL set a
server has provably never fetched, which is the only way to make both sides do
real work.
