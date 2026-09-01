"""corpus.py: passage splitting, retention, pruning, and the cached matrix.

The embedding model is only exercised where it has to be. Splitting, retention
and pruning are pure bookkeeping and are tested without it; the parts that
genuinely need vectors are marked and skipped when the weights are absent.

The matrix tests matter most. It is cached and appended to rather than rebuilt,
which is a real speed win -- a second of work at 200,000 passages, previously
paid after every write -- and also the easiest thing in this file to get subtly
wrong, because a row and its metadata must stay in step.
"""
import time

import pytest

from dethrottled import corpus as c


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DETHROTTLED_DATA_DIR", str(tmp_path))
    yield


def have_model():
    import os
    return os.path.isdir(c.ENGLISH_MODEL)


needs_model = pytest.mark.skipif(not have_model(),
                                 reason="embedding weights not downloaded")


# ── passage splitting ────────────────────────────────────────────────────────

def test_short_text_makes_no_passages():
    assert c.passages("too short") == []


def test_empty_text_is_not_fatal():
    assert c.passages("") == []
    assert c.passages(None) == []


def test_long_text_is_split():
    text = "word " * 2000
    assert len(c.passages(text)) > 1


def test_passages_overlap():
    """A fact split across a boundary is a fact neither passage can be
    retrieved for, so windows deliberately overlap."""
    text = "".join("%d " % i for i in range(2000))
    chunks = c.passages(text)
    assert len(chunks) > 1
    tail = chunks[0][-c.CHUNK_OVERLAP // 2:]
    assert any(part in chunks[1] for part in tail.split()[:5])


def test_the_title_rides_on_every_passage():
    """A paragraph three screens into an article rarely names its subject."""
    chunks = c.passages("word " * 2000, title="Okapi BM25")
    assert chunks and all(x.startswith("Okapi BM25.") for x in chunks)


def test_whitespace_is_normalised():
    assert "  " not in c.passages("a   b\n\n\nc " * 400)[0]


# ── vector packing ───────────────────────────────────────────────────────────

def test_vectors_round_trip_through_bytes():
    import numpy as np
    original = [0.5, -0.25, 0.125]
    restored = c._unpack(c._pack(original))
    assert np.allclose(restored, original)


def test_json_vectors_are_still_readable():
    """Rows written before the switch to raw float32 must still load."""
    import json

    import numpy as np
    blob = json.dumps([0.5, -0.25])
    assert np.allclose(c._unpack(blob), [0.5, -0.25])


# ── retention and pruning ────────────────────────────────────────────────────

def insert(corpus, count, age_days=0.0):
    """Write rows straight to the table: pruning is bookkeeping, not embedding."""
    import numpy as np
    rng = np.random.default_rng(0)
    when = time.time() - age_days * 86400
    rows = []
    for i in range(count):
        vector = rng.standard_normal(384).astype("float32")
        vector /= np.linalg.norm(vector)
        rows.append(("id%d-%s" % (i, age_days), "https://x/%d" % i, "t",
                     "text", corpus.model_name, c._pack(vector), when))
    corpus._db.executemany(
        "INSERT OR REPLACE INTO passages "
        "(id,url,title,text,model,vec,ts) VALUES (?,?,?,?,?,?,?)", rows)
    corpus._db.commit()
    corpus._cached = None


def count(corpus):
    return corpus._db.execute("SELECT COUNT(*) FROM passages").fetchone()[0]


def test_prune_removes_rows_past_the_age_limit():
    corpus = c.Corpus()
    insert(corpus, 5, age_days=c.RETENTION_DAYS + 10)
    insert(corpus, 5, age_days=0)
    assert corpus.prune() == 5
    assert count(corpus) == 5


def test_prune_keeps_everything_inside_the_limit():
    corpus = c.Corpus()
    insert(corpus, 10, age_days=1)
    assert corpus.prune() == 0
    assert count(corpus) == 10


def test_prune_enforces_the_cap_oldest_first(monkeypatch):
    monkeypatch.setattr(c, "MAX_PASSAGES", 8)
    corpus = c.Corpus()
    insert(corpus, 6, age_days=30)      # older
    insert(corpus, 6, age_days=1)       # newer
    corpus.prune()
    assert count(corpus) == 8
    oldest = corpus._db.execute("SELECT MIN(ts) FROM passages").fetchone()[0]
    assert oldest > time.time() - 40 * 86400


def test_prune_drops_the_cached_matrix():
    """Deletion takes rows out of the middle of the array; the cache cannot be
    patched cheaply and must be rebuilt."""
    corpus = c.Corpus()
    insert(corpus, 5, age_days=c.RETENTION_DAYS + 10)
    corpus.matrix()
    assert corpus._cached is not None
    corpus.prune()
    assert corpus._cached is None


def test_stats_reports_per_model():
    corpus = c.Corpus()
    insert(corpus, 4)
    stats = corpus.stats()
    assert stats[corpus.model_name]["passages"] == 4


def test_the_cap_is_pi_sized():
    """200,000 passages is a 307MB resident matrix before Python and the model
    have taken their share -- most of a 2GB Pi. The default has to fit the
    smallest machine this claims to run on."""
    assert c.MAX_PASSAGES <= 100000


# ── the cached matrix ────────────────────────────────────────────────────────

def test_matrix_is_cached_between_reads():
    corpus = c.Corpus()
    insert(corpus, 10)
    first = corpus.matrix()
    assert corpus.matrix() is first, "should not be rebuilt on every read"


def test_empty_corpus_returns_no_matrix():
    matrix, meta = c.Corpus().matrix()
    assert matrix is None and meta == []


@needs_model
def test_appending_matches_a_full_rebuild():
    """The correctness risk in appending: a vector and its metadata must stay
    in step. If they drift, searches return the right score attached to the
    wrong document -- which looks like working software."""
    corpus = c.Corpus()
    body = ("Sparse retrieval scores documents by term frequency and inverse "
            "document frequency across a collection. ")
    for i in range(4):
        corpus.add("https://x/%d" % i, "doc %d" % i, body * 4)
        corpus.search("anything", limit=1)          # keeps the cache warm

    query = "how are documents scored by term frequency"
    appended = [(h["url"], round(h["score"], 5))
                for h in corpus.search(query, limit=4)]
    corpus._cached = None                            # force a rebuild
    rebuilt = [(h["url"], round(h["score"], 5))
               for h in corpus.search(query, limit=4)]
    assert appended == rebuilt


@needs_model
def test_the_floor_refuses_to_answer_what_it_cannot():
    """Cosine always returns a best match. Returning nothing is the correct
    answer to "we have nothing", and much better than the least-bad thing."""
    corpus = c.Corpus()
    corpus.add("https://x/bm25", "BM25",
               "BM25 ranks documents by term frequency and inverse document "
               "frequency with length normalisation. " * 6)
    assert corpus.search("what is the capital of Peru", limit=3) == []
    assert corpus.search("how are documents ranked", limit=3)


@needs_model
def test_per_url_caps_how_much_one_page_can_fill():
    """A long article about the right subject would otherwise fill every slot
    with its own paragraphs, and a reader learns nothing from one source five
    times."""
    corpus = c.Corpus()
    corpus.add("https://x/one", "One",
               "Term frequency and document ranking. " * 200)
    hits = corpus.search("document ranking term frequency", limit=10, per_url=2)
    assert len(hits) <= 2


@needs_model
def test_reindexing_the_same_page_adds_nothing():
    corpus = c.Corpus()
    body = "Term frequency ranking of documents in a collection. " * 8
    assert corpus.add("https://x/a", "A", body) > 0
    assert corpus.add("https://x/a", "A", body) == 0
