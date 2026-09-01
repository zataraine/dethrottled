"""Semantic search over everything already fetched.

The cache became a corpus without anyone building one. Every page fetched is
kept for twenty-one days, so hundreds of real documents in several languages
are already on disk -- and the only way to find anything in them was to fetch
it again.

This indexes them. Pages are split into passages, each passage is embedded, and
a query is answered by cosine similarity over the lot.

No vector database. A few thousand passages at 384 dimensions is a matrix of a
few megabytes and a single numpy dot product; an index would be machinery with
nothing to do. Past roughly a hundred thousand passages that stops being true
and something like pgvector or FAISS earns its place -- but the interface below
need not change when it does, so that is a problem for whoever gets there.

One model: all-MiniLM-L6-v2, 87MB, ~0.3ms per passage, 384 dimensions.

multilingual-e5-small was carried alongside it and has been dropped. Measured
over ten documents on neighbouring subjects with ten questions whose right
answer was known, both scored 1.00 accuracy and 1.000 MRR -- but:

    model     acc@1    MRR    score margin    size    query
    minilm     1.00   1.000          0.242    87MB    1.4ms
    e5         1.00   1.000          0.037   465MB    3.3ms

The margin is the gap between the right answer and the best wrong one, and it
is the number that decides whether a relevance floor can be set safely at all.
MiniLM separates by 0.242; e5 compresses everything upward and separates by
0.037. Equal ranking, six times the headroom, a fifth of the size.

What is given up is cross-language retrieval -- e5 could answer a French query
from English documents. That is a real capability and this is an English-first
tool, so it goes. The `model` column in the schema stays, so adding a second
index later needs no migration.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import paths as _paths

MODELS = _paths.model_dir()

ENGLISH_MODEL = str(MODELS / "emb-minilm")


# Below this a result is not an answer, it is the least-bad thing on file.
#
# Cosine similarity always returns a best match: asked something the corpus has
# nothing for, it will still rank something first. Measured on general content,
# real answers score 0.375 to 0.585 and questions with nothing to answer them
# score 0.030 to 0.032 -- an enormous empty band, so 0.22 sits comfortably in
# the middle of it.
#
# The inherited value was 0.40, tuned on one domain-specific corpus, and it was
# cutting straight through the real answers: it rejected half of them here.
FLOOR = float(os.environ.get("DETHROTTLED_CORPUS_FLOOR", "0.22"))

# Much longer than the fetch cache's 21 days, on purpose: outliving that cache
# is the point of the corpus, because a page worth retrieving next month is
# precisely the one the cache will have dropped. But "forever" is not a policy.
RETENTION_DAYS = int(os.environ.get("DETHROTTLED_CORPUS_RETENTION_DAYS", "180"))

# 50,000, not 200,000.
#
# The cap is really a memory budget wearing a different unit. Every passage is
# a 384-dimension float32 vector, and searching means holding all of them in
# one matrix:
#
#     passages   matrix RAM   disk   rebuild   search
#       20,000        31 MB   42 MB     99ms    1.0ms
#       50,000        77 MB  105 MB    236ms    2.5ms
#      100,000       154 MB  210 MB    504ms    8.4ms
#      200,000       307 MB  419 MB   1009ms   13.8ms
#
# 200,000 asks for 307MB of resident memory before Python, the ONNX runtime and
# two embedding models have taken their share, which is most of a 2GB Pi. The
# search was never the problem -- it is 14ms even at the top of that table --
# the resident matrix is. 50,000 passages is about 12,000 pages, holds in
# 77MB, and rebuilds in a quarter of a second.
#
# Raise it if you have the memory; nothing here breaks at a larger number.
MAX_PASSAGES = int(os.environ.get("DETHROTTLED_CORPUS_MAX_PASSAGES", "50000"))

CHUNK_CHARS = int(os.environ.get("DETHROTTLED_CORPUS_CHUNK", "1200"))
CHUNK_OVERLAP = int(os.environ.get("DETHROTTLED_CORPUS_OVERLAP", "200"))

_LOADED = {}
_LOCK = threading.Lock()


def _model():
    """Load the model once per process. Returns tokenizer, session and inputs."""
    with _LOCK:
        if "en" not in _LOADED:
            import numpy
            from transformers import AutoTokenizer

            from ._quiet import load as _load_onnxruntime
            onnxruntime = _load_onnxruntime()
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = int(
                os.environ.get("DETHROTTLED_EMBED_THREADS", "4"))
            # Quiets this session's own logging. The GPU-discovery warning is
            # NOT this -- it fires during import, before any session exists,
            # which is why setting it here never suppressed it. See _quiet.py.
            options.log_severity_level = 3
            session = onnxruntime.InferenceSession(
                ENGLISH_MODEL + "/model.onnx", options,
                providers=["CPUExecutionProvider"])
            _LOADED["en"] = {
                "np": numpy,
                "tok": AutoTokenizer.from_pretrained(ENGLISH_MODEL,
                                                     trust_remote_code=False),
                "sess": session,
                "inputs": {i.name for i in session.get_inputs()},
            }
    return _LOADED["en"]


def embed(texts: list, *, batch: int = 32) -> list:
    """Unit-normalised vectors, mean-pooled over the tokens that are real.

    Pooling ignores padding: averaging over pad tokens dilutes a short passage
    towards whatever the padding embeds to, which is a slow way to make every
    document look alike.
    """
    if not texts:
        return []
    model = _model()
    np = model["np"]

    out = []
    for start in range(0, len(texts), batch):
        chunk = list(texts[start:start + batch])
        encoded = model["tok"](chunk, padding=True, truncation=True,
                               max_length=256, return_tensors="np")
        feed = {k: np.asarray(v).astype(np.int64)
                for k, v in encoded.items() if k in model["inputs"]}
        if "token_type_ids" in model["inputs"] and "token_type_ids" not in feed:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])

        hidden = model["sess"].run(None, feed)[0]
        mask = np.asarray(encoded["attention_mask"])[..., None].astype(np.float32)
        pooled = (hidden * mask).sum(1) / np.maximum(mask.sum(1), 1e-9)
        pooled /= np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9)
        out.extend(pooled.tolist())
    return out


def passages(text: str, title: str = "") -> list:
    """Split a page into overlapping passages.

    Overlapping because the sentence that answers a question has no obligation
    to sit tidily inside one window, and a fact split across a boundary is a
    fact neither passage can be retrieved for.

    The title rides on every passage: a paragraph three screens into an article
    rarely names its own subject.
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    head = ("%s. " % title.strip()) if title.strip() else ""
    step = max(CHUNK_CHARS - CHUNK_OVERLAP, 200)
    return [head + text[i:i + CHUNK_CHARS]
            for i in range(0, len(text), step)
            if len(text[i:i + CHUNK_CHARS]) > 120]


def _pack(vector) -> bytes:
    """A vector as raw float32.

    1,536 bytes against roughly 4,600 characters of JSON for the same 384
    numbers -- and no parsing back into floats on the way out.
    """
    import numpy as np
    return np.asarray(vector, dtype="float32").tobytes()


def _unpack(blob):
    """Read either form. Rows written as JSON before this change still load."""
    import numpy as np
    if isinstance(blob, (bytes, bytearray, memoryview)):
        return np.frombuffer(blob, dtype="float32")
    return np.asarray(json.loads(blob), dtype="float32")


class Corpus:
    """Passages and their vectors, in one sqlite file beside the page cache."""

    def __init__(self, path=None):
        self.path = Path(path or (_paths.data_dir() / "corpus.sqlite"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=30,
                                   check_same_thread=False)
        self._lock = threading.Lock()
        self._cached = None            # (matrix, meta), dropped when rows change
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS passages (
                    id     TEXT PRIMARY KEY,
                    url    TEXT NOT NULL,
                    title  TEXT,
                    text   TEXT NOT NULL,
                    model  TEXT NOT NULL,
                    vec    BLOB NOT NULL,
                    ts     REAL NOT NULL
                )""")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_url ON passages(url)")
            self._db.commit()

    # Kept as a column and a constant rather than removed outright: the schema
    # already separates indexes by model, so adding a second one back later
    # needs no migration and no rewrite of what is already stored.
    model_name = "minilm"

    def add(self, url: str, title: str, text: str) -> int:
        """Index one page. Returns how many passages were new."""
        chunks = passages(text, title)
        if not chunks:
            return 0
        ids = [hashlib.blake2b(("%s|%s|%d" % (url, self.model_name, i))
                               .encode("utf-8"), digest_size=12).hexdigest()
               for i in range(len(chunks))]
        with self._lock:
            known = {r[0] for r in self._db.execute(
                "SELECT id FROM passages WHERE url=? AND model=?",
                (url, self.model_name))}
        fresh = [(i, c) for i, c in zip(ids, chunks) if i not in known]
        if not fresh:
            return 0

        vectors = embed([c for _, c in fresh])
        now = time.time()
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO passages "
                "(id, url, title, text, model, vec, ts) VALUES (?,?,?,?,?,?,?)",
                [(i, url, title, c, self.model_name, _pack(v), now)
                 for (i, c), v in zip(fresh, vectors)])
            self._db.commit()
            # APPEND to the cached matrix rather than dropping it.
            #
            # Dropping it meant the next search re-read every row and rebuilt
            # the whole array: a second of work at 200,000 passages, paid after
            # every single write -- and the corpus is written on every fetch, so
            # in practice almost every search paid it. The new rows are right
            # here; adding them costs a copy proportional to what arrived, not
            # to what was already stored.
            if self._cached is not None:
                import numpy as np
                old_matrix, old_meta = self._cached
                added = np.asarray(vectors, dtype="float32")
                self._cached = (np.vstack([old_matrix, added]),
                                old_meta + [{"url": url, "title": title,
                                             "text": c} for _, c in fresh])
        return len(fresh)

    def matrix(self):
        """Every vector for this model, as one array. Built once, then reused.

        Cached because it was being rebuilt on every search: 0.30s at 2,790
        passages and a projected 2.1s at twenty thousand, spent re-reading rows
        that had not changed. Dropped whenever passages are added.
        """
        if self._cached is not None:
            return self._cached

        import numpy as np
        with self._lock:
            rows = self._db.execute(
                "SELECT url, title, text, vec FROM passages WHERE model=?",
                (self.model_name,)).fetchall()
        if not rows:
            return None, []
        meta = [{"url": r[0], "title": r[1], "text": r[2]} for r in rows]
        self._cached = (np.asarray([_unpack(r[3]) for r in rows],
                                   dtype="float32"), meta)
        return self._cached

    def prune(self) -> int:
        """Drop passages past the age limit, then past the cap. Oldest first.

        Scoped to THIS model. It used to count and delete across the whole
        table, which was right when one index existed and wrong the moment a
        second appeared: the cap is sized in passages-per-index -- 200,000 is
        about 20,000 pages -- and counting two indexes together silently halved
        the history it was chosen to hold. Deleting oldest-first across both
        would also have trimmed whichever index happened to be written first.
        """
        cutoff = time.time() - RETENTION_DAYS * 24 * 3600
        with self._lock:
            removed = self._db.execute(
                "DELETE FROM passages WHERE ts < ? AND model = ?",
                (cutoff, self.model_name)).rowcount
            total = self._db.execute(
                "SELECT COUNT(*) FROM passages WHERE model = ?",
                (self.model_name,)).fetchone()[0]
            if total > MAX_PASSAGES:
                removed += self._db.execute(
                    "DELETE FROM passages WHERE id IN ("
                    "  SELECT id FROM passages WHERE model = ?"
                    "  ORDER BY ts ASC LIMIT ?)",
                    (self.model_name, total - MAX_PASSAGES)).rowcount
            self._db.commit()
            if removed:
                # A full drop, deliberately. Deletion takes rows out of the
                # middle of the array and the bookkeeping to patch that is
                # worse than the rebuild -- and pruning runs hourly at most,
                # where writes run continuously.
                self._cached = None
        return removed

    def search(self, query: str, *, limit: int = 10, per_url: int = 2,
               floor: float | None = None, domains: list | None = None) -> list:
        """The passages closest to a query, at most `per_url` from any one page.

        Capped per page because a long article about the right subject will
        otherwise fill every slot with its own paragraphs, and a reader learns
        nothing from the same source five times.

        `floor` is what stops this answering a question it has nothing for.
        Cosine always returns a best match, and asked in French about Moroccan
        tenders -- of which this corpus holds none -- the best match was a
        Google Drive login page at 0.35, against 0.65 for a real answer to a
        real question. Returning nothing is the correct answer to "we have
        nothing", and much better than returning the least-bad thing on file.

        `domains` scopes the search to particular sites. The cache is shared by
        every subject ever queried, so a corpus built from it is cross-subject
        by construction: a question about solar capacity will otherwise happily
        return pages about semiconductor foundries, which is useful about as
        often as it is misleading.
        """
        import numpy as np
        if floor is None:
            floor = FLOOR
        vectors, meta = self.matrix()
        if vectors is None:
            return []
        q = np.asarray(embed([query])[0], dtype="float32")

        scores = vectors @ q
        wanted = tuple(d.lower() for d in (domains or ()))
        seen, out = {}, []
        for i in np.argsort(-scores):
            if scores[i] < floor:
                break                       # sorted, so nothing below clears it
            row = meta[i]
            if wanted and not any(d in row["url"].lower() for d in wanted):
                continue
            if seen.get(row["url"], 0) >= per_url:
                continue
            seen[row["url"]] = seen.get(row["url"], 0) + 1
            out.append(dict(row, score=float(scores[i])))
            if len(out) >= limit:
                break
        return out

    def stats(self) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT model, COUNT(*), COUNT(DISTINCT url) FROM passages "
                "GROUP BY model").fetchall()
        return {r[0]: {"passages": r[1], "pages": r[2]} for r in rows}


# How much of a page the corpus takes, regardless of how much was fetched.
# These are two different jobs: a caller wants the whole article, the index
# wants the part of it that is about something. A page's tail is its footer,
# its comments and its related-articles, and indexing those makes them
# retrievable -- the corpus would start answering questions with boilerplate.
INDEX_CHARS = int(os.environ.get("DETHROTTLED_CORPUS_INDEX_CHARS", "4000"))


# Retention only means something if it runs. prune() had no callers at all --
# neither the 180-day limit nor the 200,000 cap was ever applied to anything --
# which mattered little while the corpus was frozen and matters now that it
# grows on every fetch. Once an hour is often enough for limits measured in
# months, and keeps a DELETE scan off the common path.
PRUNE_EVERY = int(os.environ.get("DETHROTTLED_CORPUS_PRUNE_SECONDS", "3600"))
_last_prune = 0.0


def index_fetched(fetched) -> int:
    """Add freshly fetched bodies to the corpus. Best effort, never fatal.

    Lives here rather than in provider.py, where it used to, because provider
    is only one of two callers and was the quieter one. The HTTP routes fetch
    far more pages than any batch job does, and indexed none of them: the corpus
    sat at 2790 passages across 617 urls while the extract cache grew past a
    thousand entries. A retrieval index that the main interface does not feed
    is a retrieval index that slowly stops being about anything.

    Takes (url, result) pairs. `result` may be a fetcher result (ok/text) or an
    API row (quality/content), because both callers have one of those and
    neither should have to reshape it.
    """
    global _last_prune
    if os.environ.get("DETHROTTLED_CORPUS_AUTOINDEX", "1") != "1":
        return 0
    due = (time.time() - _last_prune) > PRUNE_EVERY
    added = 0
    try:
        corpus = Corpus()
    except Exception:
        return 0
    if due:
        try:
            corpus.prune()
        except Exception:
            pass
    for url, result in fetched:
        try:
            ok = result.get("ok") or result.get("quality") == "ok"
            if not ok:
                continue
            body = result.get("text") or result.get("content") or ""
            if body:
                corpus.add(url, result.get("title", ""), body[:INDEX_CHARS])
                added += 1
        except Exception:
            continue
    if due:
        _last_prune = time.time()
    return added
