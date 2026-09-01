# Security

## The thing to understand first

**dethrottled fetches arbitrary URLs on behalf of whoever can reach it.**

That is its job, and it is also the whole risk. Anyone who can send it a request
can make it issue HTTP requests from your machine, to any address it can route
to — including your own private network, your cloud provider's metadata
endpoint, and anything else behind your firewall. That is server-side request
forgery, and this service is a very effective engine for it if left open.

So it binds to `127.0.0.1` by default, the Docker port mapping publishes to
`127.0.0.1` only, and SearXNG and Crawl4AI are not published to the host at all.

## Exposing it to a network

If other machines need to reach it, that is a deliberate decision and it needs
three things, not one:

1. **Authentication in front of it.** A reverse proxy requiring a credential.
   There is no auth in dethrottled itself, and adding a token would only look
   like security.
2. **A network allowlist.** Restrict source addresses at the proxy or firewall.
   `--host 0.0.0.0` with nothing in front is an open proxy.
3. **Egress restrictions**, if you care about SSRF. Block the service from
   reaching link-local (`169.254.0.0/16`, where cloud metadata lives) and your
   own RFC1918 ranges, unless it genuinely needs them.

```bash
dethrottled --host 0.0.0.0 --port 8787    # only behind the above
```

## What leaves your machine

| tier | contacts | default |
| --- | --- | --- |
| `direct` | the site you asked for | on |
| `tls` | the site you asked for | on |
| `crawl4ai` | your own container | on if configured |
| `jina-reader` | **`r.jina.ai`, a third party** | **off** |

`jina-reader` is the only tier that tells anybody else what you are reading. It
is free and keyless, but **the URL is the payload** — if the URLs you fetch are
sensitive, leave it off:

```bash
DETHROTTLED_ENABLE_JINA=0
```

The compose stack runs with it off and loses very little, because Crawl4AI
renders JavaScript locally.

Two other outbound paths worth knowing about:

- **video transcripts** contact YouTube directly, with the video ID. Disable
  with `DETHROTTLED_ENABLE_TRANSCRIPTS=0`
- **web search** contacts the search engines you have enabled, with your query.
  Self-hosting SearXNG keeps queries in-network

There is deliberately **no relay tier**. Every option needed either an account,
or a third party who then learns every URL you fetch, or an address range that
anti-bot vendors blocklist on sight.

## Secrets

There are none. Nothing dethrottled talks to has a credential, which is the
entire point — there is no key to leak, rotate, or accidentally commit.

## What is stored on disk

Page bodies, search results, the passage index, engine health and domain
health — in the data directory, `~/.cache/dethrottled` unless you set
`DETHROTTLED_DATA_DIR`. That is other people's content and your own query
history, in SQLite and JSON files with whatever permissions your umask gave
them.

It is all disposable. Delete any of it and the system refills it.

Note that the **page source is not stored** — the cache keeps extracted text
only. That is a size decision (HTML averages 47× its text) but it also means
the cache holds rather less than you might assume.

## Fetching is bounded on purpose

Limits exist so one pathological page cannot cost a minute or a gigabyte:

- 10MB HTML ceiling, 25MB PDF, 50MB other documents
- a whole-page time budget checked between tiers — 60s named URL, 25s bulk
- hourly per-tier volume budgets on the renderer and the external reader
- OCR capped at 8 pages per document, 30s per page
- corpus capped at 50,000 passages and 180 days

## Politeness is a security property too

robots.txt is honoured at every tier and cached; one request per domain at a
time with a 1.5s floor; an honest, contactable User-Agent. A relay is a
different route to the same publisher, not permission to ignore what they
asked for.

If you change the User-Agent, **say what you are and leave a way to be told to
stop**. The default names the project and points at the repository.

## Reporting a vulnerability

Open a GitHub issue for anything that is not itself sensitive. For something
that should not be public, use GitHub's private security advisory reporting on
this repository.
