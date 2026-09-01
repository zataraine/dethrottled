"""The API contract: every route returns the documented fields, offline.

A published response shape is a promise, and promises are worth testing. This
stubs the network layer entirely, so it asserts the SHAPE of every route
without depending on the live web, on a search engine being reachable, or on
anything being installed beyond the base package. That is what makes it
runnable in CI on four Python versions and two architectures.

What it deliberately does not test: whether the results are any good. That is
what the benchmark and the live tier matrix are for.
"""
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from dethrottled import search as fs  # noqa: E402
from dethrottled import server as srv  # noqa: E402

SEARCH_ROW_FIELDS = {
    "url", "title", "snippet", "publishedDate", "engine", "engines",
    "category", "score", "cached", "search_attempts", "search_elapsed_ms",
    "ranking", "from_corpus",
}
EXTRACT_ROW_FIELDS = {"url", "content", "content_type", "quality", "tier", "cached"}

FAKE_ROWS = [
    {"url": "https://example.com/one", "title": "One", "snippet": "first",
     "publishedDate": "", "engine": "bing-news"},
    {"url": "https://example.com/two", "title": "Two", "snippet": "second",
     "publishedDate": "", "engine": "bing-news"},
]
FAKE_META = {"ok": True, "count": 2, "per_source": {"bing-news": 2},
             "elapsed_ms": 12, "cost": "free"}


@pytest.fixture
def client(monkeypatch):
    """A server whose network layer answers instantly and never leaves the box."""
    answer = lambda *a, **k: (list(FAKE_ROWS), dict(FAKE_META))  # noqa: E731
    monkeypatch.setattr(fs, "search", answer)
    monkeypatch.setattr(srv.fs, "search",
                        lambda *a, **k: (list(FAKE_ROWS), dict(FAKE_META)))
    monkeypatch.setattr(srv.fetcher, "fetch_and_extract", lambda url, **k: {
        "ok": True, "text": "body text for " + url, "tier": "direct",
        "extractor": "trafilatura", "title": "T", "published": "",
        "chars": 20, "url": url, "reason": "", "cached": False,
        "content_type": "html"})
    # Indexing is a background task and needs no model in a contract test.
    monkeypatch.setattr(srv, "index_fetched", lambda *a, **k: 0)
    return TestClient(srv.app)


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "dethrottled"
    assert "version" in body


def test_capabilities_reports_only_what_is_configured(client):
    body = client.get("/v2/capabilities").json()
    assert body["keys_required"] is False
    assert body["quotas"] is None
    # direct always exists; the two that need your own infrastructure must not
    # be advertised unless they are actually configured.
    assert "direct" in body["fetch_tiers"]
    assert "bing-news-rss" in body["search"]
    # One embedding model and one reranker, both permissively licensed. No
    # non-commercial component appears anywhere in this list.
    assert set(body["ranking"]) == {
        "bm25", "corpus", "rerank"}


def test_status_distinguishes_unconfigured_from_down(client, monkeypatch):
    monkeypatch.setattr(srv.fs, "SEARXNG_URL", "")
    monkeypatch.setattr(srv.fs, "health", lambda: {
        "ok": True, "searxng": False, "bing_news": True, "detail": ""})
    body = client.get("/v2/status").json()
    assert body["components"]["searxng"]["status"] == "not_configured"


def test_search_row_shape(client):
    rows = client.post("/search", json={"query": "anything", "num_results": 2}).json()
    assert rows, "expected rows"
    for r in rows:
        assert SEARCH_ROW_FIELDS <= set(r), SEARCH_ROW_FIELDS - set(r)


def test_search_telemetry_is_on_the_first_row_only(client):
    """Callers read search_attempts once. Repeating it on every row would
    multiply a fixed cost by the result count for no added information."""
    rows = client.post("/search", json={"query": "anything", "num_results": 2}).json()
    assert rows[0]["search_attempts"]
    for r in rows[1:]:
        assert r["search_attempts"] == []


def test_search_reports_which_ranking_stages_ran(client):
    rows = client.post("/search", json={"query": "anything", "rank": True}).json()
    assert "bm25" in rows[0]["ranking"]
    rows = client.post("/search", json={"query": "anything", "rank": False}).json()
    assert rows[0]["ranking"] == []


def test_search_respects_the_result_limit(client):
    rows = client.post("/search", json={"query": "x", "num_results": 1}).json()
    assert len(rows) == 1


def test_extract_row_shape(client):
    rows = client.post("/extract", json={"urls": ["https://example.com/a"]}).json()
    assert len(rows) == 1
    assert EXTRACT_ROW_FIELDS <= set(rows[0])
    assert rows[0]["quality"] == "ok"
    assert rows[0]["tier"] == "direct/trafilatura"


def test_extract_reports_failure_without_raising(client, monkeypatch):
    """A page that cannot be fetched is a row with a reason, not a 500. One bad
    URL in a batch must not lose the whole response."""
    monkeypatch.setattr(srv.fetcher, "fetch_and_extract", lambda url, **k: {
        "ok": False, "reason": "robots_disallow", "cached": False})
    rows = client.post("/extract", json={"urls": ["https://example.com/a"]}).json()
    assert rows[0]["quality"] == "failed"
    assert rows[0]["failure_reason"]
    assert rows[0]["content"] == ""


def test_extract_of_nothing_is_an_empty_list(client):
    assert client.post("/extract", json={"urls": []}).json() == []


def test_search_and_extract_carries_both_shapes(client):
    rows = client.post("/search-and-extract",
                       json={"query": "anything", "num_results": 2}).json()
    assert rows
    for r in rows:
        assert SEARCH_ROW_FIELDS <= set(r)
        assert "content" in r and "quality" in r


def test_corpus_routes_are_not_fatal_without_a_model(client):
    """The semantic extra is optional, so these must answer rather than 500
    when it is absent."""
    body = client.get("/corpus/stats").json()
    assert "ok" in body and "models" in body
    body = client.get("/corpus/search", params={"q": "anything"}).json()
    assert "ok" in body and "results" in body


def test_stats_reports_tier_budgets(client):
    """Budget exhaustion used to be invisible. It is surfaced deliberately."""
    body = client.get("/stats").json()
    assert "tiers" in body and "tier_budgets" in body
    assert "tier_budget_used" in body
