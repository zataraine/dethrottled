"""Ranking: put the answer near the top before anything expensive happens.

Search sources return rows in whatever order they feel like, and merging three
of them produces an order that means nothing at all. This orders the pool, in
up to three stages, each optional and each degrading to the one before it:

    1. BM25            lexical, no model, no dependencies, microseconds
    2. corpus merge    passages already fetched, added to the pool
    3. cross-encoder   a real model, but only over a shortlist

The ordering of the stages is the design, and both orderings in it were chosen
on measurement:

**Rank before fetching.** Fetching is the expensive step -- seconds per page
against microseconds to rank -- and ranking does not need page bodies. Titles
beat bodies for ranking anyway (see rank_rows). So the pool is ordered first
and only the winners are ever fetched.

**Merge the corpus before ranking**, not after. A corpus passage and a web
result are both candidate answers, and appending the corpus to the end of a
ranked list quietly declares every corpus hit worse than every web hit. Merging
first makes the cross-encoder judge them on equal terms.

Nothing here is required. With no model installed, ranking is BM25 and the
service works. With no corpus built, the pool is what the web returned. Each
stage announces itself once when it cannot run, and then stops mentioning it.
"""
from __future__ import annotations

import math
import os
import re

from . import paths as _paths

_TOKEN = re.compile(r"[a-z0-9]+")


def _log(message: str) -> None:
    if os.environ.get("DETHROTTLED_QUIET") != "1":
        print(message)


# ── stage 1: BM25 ────────────────────────────────────────────────────────────

def rank_rows(rows: list, want: str, *, recency: float = 0.0,
              half_life: float = 21.0) -> list:
    """Order a pool by how well each row answers `want`. BM25, no model.

    Chosen on measurement rather than fashion: against a neural bi-encoder on
    the pool this was written for, BM25 placed more useful rows in the top 14
    and did it in no time at all rather than 167 seconds. Rare words are what
    discriminate between search results -- the specific noun, the model number,
    the place name -- and a bi-encoder trained for semantic similarity prefers
    documents that are broadly on-topic to documents that contain the answer.

    `recency` blends in freshness for callers who want the last fortnight
    rather than the best reference page. Multiplicative and bounded, never
    additive: freshness may reorder results that already earned a relevance
    score, and may never promote an irrelevant one. Undated rows count as
    neutral rather than old, because the single most useful result in the case
    this was built for was an undated list.
    """
    if not rows or not want:
        return rows

    terms = [w for w in _TOKEN.findall(want.lower()) if len(w) > 2]
    if not terms:
        return rows

    # Scored on the title and the opening of the text, not the whole page.
    # Measured: ranking on 3000 characters of extracted body and on
    # title-plus-240 both put 5 of 10 useful rows in the top 14, but the short
    # form put the best one at rank 1 rather than rank 3. The rest of a page is
    # navigation, cookie notice and footer, and all of it votes.
    #
    # This is only what RANKING sees. Whatever wins is still returned in full.
    docs = [_TOKEN.findall(
        ("%s %s" % (r.get("title", ""), (r.get("text") or "")[:240])).lower())
        for r in rows]
    count = len(docs)
    average = sum(len(d) for d in docs) / (count or 1)
    seen_in = {t: sum(1 for d in docs if t in d) or 1 for t in terms}

    scored = []
    for index, doc in enumerate(docs):
        length = len(doc) or 1
        score = 0.0
        for term in terms:
            freq = doc.count(term)
            if not freq:
                continue
            idf = math.log(1 + (count - seen_in[term] + 0.5) / (seen_in[term] + 0.5))
            norm = 0.25 + 0.75 * length / average
            score += idf * (freq * 2.5) / (freq + 1.5 * norm)
        if recency > 0:
            age = age_days(rows[index])
            fresh = 1.0 if age is None else 0.5 ** (age / half_life)
            score *= (1.0 - recency) + recency * fresh
        scored.append((score, index))

    scored.sort(key=lambda pair: -pair[0])
    return [rows[i] for _, i in scored]


def age_days(row: dict):
    """Age of a result in days, or None when it does not say.

    Two formats because search results arrive in both: RSS gives RFC 2822 and
    everything modern gives ISO 8601. A row whose date cannot be parsed is
    undated, not ancient -- guessing old would bury it under `recency`.
    """
    raw = (row.get("published") or row.get("publishedDate") or "").strip()
    if not raw:
        return None
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    for parse in (parsedate_to_datetime,
                  lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))):
        try:
            when = parse(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - when).days)
        except (ValueError, TypeError):
            continue
    return None


# ── stage 2: the corpus ──────────────────────────────────────────────────────

def add_corpus(rows: list, want: str, *, limit: int = 10) -> list:
    """Merge corpus passages into the pool, skipping URLs already in it.

    Passages arrive with their text already extracted, so they cost no fetch --
    which makes a corpus row cheaper than a web row, not merely extra.

    Never fatal: an unbuilt or unreadable corpus leaves the pool exactly as the
    web returned it.
    """
    if not want or limit <= 0:
        return rows
    try:
        from .corpus import Corpus
        corpus = Corpus()
        hits = corpus.search(want, limit=limit)
    except Exception as exc:
        _log("  corpus unavailable (%s); web results only" % str(exc)[:60])
        return rows

    # An index with nothing in it returns [] exactly like an index with no
    # match, and that silence hid a real bug once: an index that nothing wrote
    # to returned nothing forever, with no indication of why. "No match" and
    # "nothing to match against" are different answers and must not look the
    # same.
    if not hits:
        try:
            # stats() is keyed by MODEL, not flat -- reading it flat would
            # report 0 always and cry "empty" on an honest no-match.
            held = corpus.stats().get(corpus.model_name, {}).get("passages", 0)
        except Exception:
            held = -1
        if held == 0:
            _log("  corpus is EMPTY -- nothing has been indexed yet")
        return rows

    have = {r.get("url") for r in rows}
    added = [{"url": h["url"], "title": h["title"] or "", "text": h["text"],
              "snippet": h["text"][:240], "published": "", "engine": "corpus",
              "from_corpus": True, "score": h.get("score")}
             for h in hits if h["url"] not in have]
    if added:
        _log("  +%d from the corpus that search did not return" % len(added))
    return rows + added


# ── stage 3: the cross-encoder ───────────────────────────────────────────────

_MODELS = _paths.model_dir()

# ms-marco-MiniLM-L-12-v2 (Apache-2.0), fetched and cached by flashrank on
# first use. ~6ms/doc, 21MB.
#
# English only, and deliberately so, for two separate reasons.
#
# Licensing: the obvious multilingual cross-encoder,
# jina-reranker-v2-base-multilingual, is CC-BY-NC-4.0. A non-commercial
# component has no place in an MIT repository even as an optional one --
# "optional" is not something a licence audit can rely on.
#
# Weight: this is an English-first tool. Measured, the smaller English
# embedding model matched the multilingual one on accuracy and ranking while
# separating right answers from wrong ones six times more cleanly, at a fifth
# of the size. Carrying a second model to serve a case this tool does not
# claim to cover is half a gigabyte for nothing.
#
# BM25 is language-agnostic, so a non-English pool is still ordered. It is the
# second stage, and only that, which is English-only.
XENC_MODEL = os.environ.get("DETHROTTLED_XENC_MODEL", "ms-marco-MiniLM-L-12-v2")
XENC_CACHE = os.environ.get("DETHROTTLED_XENC_CACHE", str(_MODELS / "flashrank"))

# How many rows the cross-encoder actually sees. It pays per document, and
# measured, reranking a whole 189-row pool cost eleven times as much as
# reranking the top forty for the same answer. BM25 has already ordered the
# pool; this only fixes the top of it.
XENC_SHORTLIST = int(os.environ.get("DETHROTTLED_XENC_SHORTLIST", "40"))

_XENC = {}
_XENC_BROKEN = []


def _score_english(want: str, texts: list) -> list:
    """ms-marco-MiniLM-L-12-v2 via flashrank. Fast, and English only."""
    # flashrank imports onnxruntime itself, so get it in first -- otherwise the
    # GPU-discovery warning is printed by flashrank's import instead of being
    # swallowed by ours.
    from ._quiet import load as _load_onnxruntime
    _load_onnxruntime()
    from flashrank import Ranker, RerankRequest
    if "en" not in _XENC:
        _XENC["en"] = Ranker(model_name=XENC_MODEL, cache_dir=XENC_CACHE)
    ranked = _XENC["en"].rerank(RerankRequest(
        query=want,
        passages=[{"id": i, "text": text} for i, text in enumerate(texts)]))
    scores = [0.0] * len(texts)
    for row in ranked:
        scores[row["id"]] = row["score"]
    return scores


def cross_encode(rows: list, want: str, *, shortlist: int = 0) -> list:
    """Re-order the top `shortlist` rows with a cross-encoder, or return as-is.

    A cross-encoder reads query and document together, with attention across
    both, which is why it beats a bag of words and why it costs per document.
    So it never sees the whole pool.

    Silent about being unavailable after the first time. This is an improvement
    to ranking, not a dependency of it: if the model will not load, ranking
    stays lexical and the request carries on.
    """
    shortlist = shortlist or XENC_SHORTLIST
    if not rows or not want:
        return rows
    if "en" in _XENC_BROKEN:
        return rows

    head, tail = rows[:shortlist], rows[shortlist:]
    texts = [("%s %s" % (r.get("title", ""),
                         (r.get("text") or r.get("snippet") or "")[:400])).strip()
             for r in head]
    try:
        scores = _score_english(want, texts)
    except Exception as exc:
        # Written off once, then silent. A missing reranker is a smaller
        # stack, not a broken one, and saying so on every request would bury
        # the fact in noise.
        _XENC_BROKEN.append("en")
        _log("  cross-encoder unavailable (%s); ranking stays lexical"
             % str(exc)[:60])
        return rows

    order = sorted(range(len(head)), key=lambda i: -scores[i])
    return [head[i] for i in order] + tail


# ── what is actually available ───────────────────────────────────────────────

def available() -> dict:
    """Which ranking stages this installation can actually run.

    Reported by /v2/capabilities so a caller can tell "reranking is off"
    from "reranking is on and silently doing nothing", which was the failure
    mode that motivated most of the logging in this module.
    """
    import importlib.util
    import os.path
    have_ort = importlib.util.find_spec("onnxruntime") is not None
    have_models = have_ort and importlib.util.find_spec("transformers") is not None
    return {
        "bm25": True,
        # The runtime being installed and the weights being on disk are
        # separate questions, and only the second one is a download.
        "corpus": have_models and os.path.isdir(str(_MODELS / "emb-minilm")),
        # The LIBRARY being importable is not the same as the model being
        # present, and conflating them is the failure this project exists to
        # avoid. flashrank downloads its weights on first use, so in an offline
        # or air-gapped deployment `available()` reported True and the first
        # rerank then quietly fell back to lexical ordering.
        #
        # Reported as ready only when both are true.
        "rerank": (importlib.util.find_spec("flashrank") is not None
                   and _rerank_weights_present()),
    }


def _rerank_weights_present() -> bool:
    """Has flashrank's model actually been downloaded?

    It caches under a directory named for the model. Checking for the directory
    is enough: flashrank writes it only after a successful extraction.
    """
    import os
    return os.path.isdir(os.path.join(XENC_CACHE, XENC_MODEL))


def apply(rows: list, want: str, *, bm25: bool = True, rerank: bool = False,
          corpus: int = 0, recency: float = 0.0) -> tuple:
    """The whole ladder, in the one order that makes sense.

    Returns (rows, stages) where `stages` names what actually ran -- not what
    was asked for. A caller that requested reranking and got lexical ordering
    because no model was installed should be able to see that in the response
    rather than infer it from disappointing results.
    """
    stages = []
    if not want:
        return rows, stages
    if corpus > 0:
        before = len(rows)
        rows = add_corpus(rows, want, limit=corpus)
        if len(rows) != before:
            stages.append("corpus")
    if bm25:
        rows = rank_rows(rows, want, recency=recency)
        stages.append("bm25+recency" if recency > 0 else "bm25")
    if rerank:
        top = rows[0].get("url") if rows else None
        rows = cross_encode(rows, want)
        if "en" not in _XENC_BROKEN:
            stages.append("cross-encoder")
            if rows and rows[0].get("url") != top:
                _log("    cross-encoder reordered the top result")
    return rows, stages
