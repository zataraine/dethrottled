# Working on dethrottled

## One tree

The repository lives in **one place**: a Linux working copy, with the
virtualenv beside it. There is deliberately no second copy on another
filesystem.

That is not a style preference. This project was developed for a while with a
git tree on one filesystem and a test tree on another, synced by copying files
between them, and it cost two regressions -- a README section reverted by a
sync in one direction, and a version bump applied to the copy that was not the
one being released. Both were caught, neither should have been possible.

If you need the source visible from another OS, mount it. Do not copy it.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[all,dev]"
./scripts/fetch-models.sh          # 87MB, only needed for the corpus
```

## Before you push

```bash
.venv/bin/python -m pytest tests/ -q -m "not network"
.venv/bin/python -m ruff check src tests
```

Network-marked tests hit the live web and are excluded from CI on purpose: a
failure there should mean this code is wrong, not that a publisher was having
a bad morning. Run them by hand when you touch a fetch tier:

```bash
.venv/bin/python -m pytest -m network -q -s
```

## The docker stack

```bash
cp .env.example .env
docker compose up -d
```

Note that Crawl4AI binds to loopback unless it has a credential, so the token
in `.env` is what lets the API container reach it at all. See
[TLDREADME.md](TLDREADME.md) §4.

## What this project values

**Measure, then decide.** Almost every number in the documentation came from a
script in `scripts/`, and several of them overturned an assumption that looked
obvious -- BeautifulSoup was the slowest extractor *and* the dirtiest, the
larger embedding model ranked no better than the small one, and four sites
written up as IP-blocked turned out to be serving an ordinary challenge that a
real browser also received.

**A negative result is a result.** [TLDREADME.md](TLDREADME.md) §18 and §19
document what was rejected and what does not work, with the measurements. That
is not an apology section; it is the most useful part of the documentation.

**Report capability honestly.** A tier that answers but returns nothing usable
is `DEGRADED`, not `OK`. A component nobody configured is `OFF`, not `DEAD`. If
a capability cannot actually run, `/v2/capabilities` says so rather than
reporting that the library imported.

**Guard every optional import.** A missing extra removes a capability; it must
never break one. The base install has to keep working on a machine with none of
the extras present -- CI runs macOS and Windows that way on purpose.
