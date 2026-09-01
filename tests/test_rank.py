"""BM25 ordering and the recency blend.

The recency tests are the important ones. Freshness is multiplicative and
bounded precisely so that it can reorder results which already earned a
relevance score and can never promote one that did not -- an additive bonus
would let a brand-new page about nothing outrank a week-old page that answers
the question. That property is easy to break and invisible when broken, so it
is asserted directly.
"""
from datetime import datetime, timedelta, timezone

from dethrottled.rank import age_days, apply, rank_rows


def row(title, text="", published=""):
    return {"url": "https://example.com/" + title.replace(" ", "-").lower(),
            "title": title, "text": text, "published": published}


def rfc2822(days_ago):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return when.strftime("%a, %d %b %Y %H:%M:%S +0000")


def titles(rows):
    return [r["title"] for r in rows]


def test_ranks_the_matching_document_first():
    pool = [row("Unrelated gardening advice"),
            row("Okapi BM25 ranking function"),
            row("Weather forecast")]
    assert titles(rank_rows(pool, "BM25 ranking"))[0] == "Okapi BM25 ranking function"


def test_rare_terms_beat_common_ones():
    """The whole reason BM25 was chosen over a bi-encoder here: the rare word
    is the one that discriminates."""
    pool = [row("The the the the ranking the"),
            row("Sparse retrieval with BM25")]
    assert titles(rank_rows(pool, "BM25"))[0] == "Sparse retrieval with BM25"


def test_empty_query_leaves_the_pool_alone():
    pool = [row("A"), row("B")]
    assert rank_rows(pool, "") == pool


def test_query_of_only_short_words_leaves_the_pool_alone():
    """Terms of 2 characters or fewer are dropped; a query made entirely of
    them has nothing to rank on and must not silently reorder anything."""
    pool = [row("A"), row("B")]
    assert rank_rows(pool, "a an in") == pool


def test_recency_reorders_equally_relevant_rows():
    old = row("BM25 ranking", published=rfc2822(200))
    new = row("BM25 ranking guide", published=rfc2822(1))
    ranked = rank_rows([old, new], "BM25 ranking", recency=0.9)
    assert ranked[0]["published"] == new["published"]


def test_recency_never_promotes_an_irrelevant_row():
    """The property that makes the blend safe. A fresh page about nothing must
    stay below a stale page that actually answers the query, at any weight."""
    relevant = row("Okapi BM25 ranking function", published=rfc2822(400))
    fresh_junk = row("Today's lunch menu", published=rfc2822(0))
    for weight in (0.1, 0.5, 0.9, 1.0):
        ranked = rank_rows([fresh_junk, relevant], "BM25 ranking", recency=weight)
        assert ranked[0]["title"] == "Okapi BM25 ranking function", weight


def test_undated_rows_count_as_neutral_not_ancient():
    """Guessing 'old' for a missing date would bury undated reference pages,
    which are often the best result."""
    undated = row("BM25 ranking function")
    dated_old = row("BM25 ranking function", published=rfc2822(500))
    ranked = rank_rows([dated_old, undated], "BM25 ranking function", recency=0.9)
    assert ranked[0]["title"] == "BM25 ranking function"
    assert not ranked[0].get("published")


def test_age_days_reads_both_date_formats():
    assert 0 <= age_days({"published": rfc2822(3)}) <= 4
    iso = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert 0 <= age_days({"published": iso}) <= 4


def test_age_days_is_none_when_absent_or_unparseable():
    assert age_days({}) is None
    assert age_days({"published": "last Tuesday-ish"}) is None


def test_apply_reports_the_stages_that_actually_ran():
    """A caller that asked for reranking and got lexical ordering should be
    able to see that, rather than infer it from disappointing results."""
    pool = [row("BM25"), row("Other")]
    _, stages = apply(pool, "BM25", bm25=True, rerank=False, corpus=0)
    assert stages == ["bm25"]

    _, stages = apply(pool, "BM25", bm25=True, rerank=False, corpus=0, recency=0.5)
    assert stages == ["bm25+recency"]


def test_apply_with_no_query_does_nothing():
    pool = [row("A"), row("B")]
    out, stages = apply(pool, "")
    assert out == pool and stages == []
