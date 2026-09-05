"""The endpoint surface: two verbs, one combination, and the aliases.

The shape is a deliberate decision and worth pinning. There is no `/crawl`,
because rendering is a strategy for obtaining a page rather than something a
caller wants for its own sake -- a caller who picked the renderer by hand would
spend four seconds of Chromium on pages `direct` serves in two hundred
milliseconds. Forcing the question is the `render` parameter instead.

The aliases matter just as much: callers already exist that were written
against `/extract`, and a rename for tidiness would break them for nothing.
"""
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from dethrottled import server as srv  # noqa: E402

FAKE_ROWS = [
    {"url": "https://example.com/one", "title": "One", "snippet": "first",
     "publishedDate": "", "engine": "web-duckduckgo"},
]
FAKE_META = {"ok": True, "count": 1, "per_source": {"web": 1},
             "elapsed_ms": 5, "cost": "free"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(srv.fs, "search",
                        lambda *a, **k: (list(FAKE_ROWS), dict(FAKE_META)))
    monkeypatch.setattr(srv, "index_fetched", lambda *a, **k: 0)

    def fake_fetch(url, **kw):
        row = {"ok": True, "text": "body text", "tier": "direct",
               "extractor": "trafilatura", "title": "T", "published": "",
               "chars": 9, "url": url, "reason": "", "cached": False,
               "content_type": "html", "_render": kw.get("allow_render")}
        if kw.get("keep_html"):
            row["html"] = "<html>source</html>"
        return row

    monkeypatch.setattr(srv.fetcher, "fetch_and_extract", fake_fetch)
    return TestClient(srv.app)


# ── the surface ──────────────────────────────────────────────────────────────

def test_there_is_no_crawl_endpoint():
    """Rendering is a parameter, not a route. If this ever starts passing as a
    404-not-found-because-someone-added-it, read the module docstring first."""
    paths = {r.path for r in srv.app.routes}
    assert "/crawl" not in paths
    assert {"/search", "/fetch", "/search-and-fetch"} <= paths


def test_extract_aliases_are_still_mounted():
    """Existing callers were written against these names."""
    paths = {r.path for r in srv.app.routes}
    assert "/extract" in paths
    assert "/search-and-extract" in paths


@pytest.mark.parametrize("path", ["/fetch", "/extract"])
def test_fetch_and_its_alias_behave_identically(client, path):
    rows = client.post(path, json={"urls": ["https://example.com/a"]}).json()
    assert rows[0]["quality"] == "ok"
    assert rows[0]["content"] == "body text"


@pytest.mark.parametrize("path", ["/search-and-fetch", "/search-and-extract"])
def test_combined_and_its_alias_behave_identically(client, path):
    rows = client.post(path, json={"query": "anything", "num_results": 1}).json()
    assert rows and rows[0]["content"] == "body text"


# ── raw ──────────────────────────────────────────────────────────────────────

def test_raw_returns_the_source(client):
    rows = client.post("/fetch", json={"urls": ["https://example.com/a"],
                                       "raw": True}).json()
    assert rows[0]["html"] == "<html>source</html>"


def test_normal_fetch_omits_the_source(client):
    """The default response must not carry the page source: it is often twenty
    times the size of the text, and nobody asked for it."""
    rows = client.post("/fetch", json={"urls": ["https://example.com/a"]}).json()
    assert "html" not in rows[0]


# ── render control ───────────────────────────────────────────────────────────

def test_render_never_forbids_the_renderer(client):
    """`render` says what the ladder is ALLOWED to do, not what it must do."""
    captured = {}
    original = srv.fetcher.fetch_and_extract

    def spy(url, **kw):
        captured["allow_render"] = kw.get("allow_render")
        return original(url, **kw)

    srv.fetcher.fetch_and_extract = spy
    try:
        client.post("/fetch", json={"urls": ["https://example.com/a"],
                                    "render": "never"})
        assert captured["allow_render"] is False
        client.post("/fetch", json={"urls": ["https://example.com/a"]})
        assert captured["allow_render"] is True
    finally:
        srv.fetcher.fetch_and_extract = original


def test_capabilities_lists_the_web_engines(client):
    """"web search" is not one source, and a caller debugging an empty result
    wants to know which engines were even asked."""
    body = client.get("/v2/capabilities").json()
    assert any(name.startswith("web-") for name in body["search"])


def test_render_always_puts_the_renderer_first(client):
    """ exists for the page whose static HTML has SOME text but whose
    real content needs JavaScript -- there,  never asks the renderer."""
    seen = {}
    original = srv.fetcher.fetch_and_extract

    def spy(url, **kw):
        seen["render_first"] = kw.get("render_first")
        seen["allow_render"] = kw.get("allow_render")
        return original(url, **kw)

    srv.fetcher.fetch_and_extract = spy
    try:
        client.post("/fetch", json={"urls": ["https://example.com/a"],
                                    "render": "always"})
        assert seen == {"render_first": True, "allow_render": True}
        client.post("/fetch", json={"urls": ["https://example.com/a"],
                                    "render": "auto"})
        assert seen == {"render_first": False, "allow_render": True}
        client.post("/fetch", json={"urls": ["https://example.com/a"],
                                    "render": "never"})
        assert seen == {"render_first": False, "allow_render": False}
    finally:
        srv.fetcher.fetch_and_extract = original


# -- what real clients actually send -----------------------------------------

def test_search_accepts_language_and_forwards_it(monkeypatch):
    """`language` reaches the engine, rather than being accepted and dropped.

    It was neither for a while: the engine honoured it, the HTTP body did not
    offer it, and once unknown fields became a 422 every client that sent it
    broke outright. An OpenClaw plugin sends it on every native search.
    """
    seen = {}

    def fake_search(query, **kw):
        seen.update(kw)
        return list(FAKE_ROWS), dict(FAKE_META)

    monkeypatch.setattr(srv.fs, "search", fake_search)
    monkeypatch.setattr(srv, "index_fetched", lambda *a, **k: 0)
    c = TestClient(srv.app)

    assert c.post("/search", json={"query": "q", "language": "fr"}).status_code == 200
    assert seen.get("language") == "fr"


def test_absent_language_is_not_forwarded_as_empty(monkeypatch):
    """No language means no hint -- not an empty string to be interpreted."""
    seen = {}

    def fake_search(query, **kw):
        seen.update(kw)
        return list(FAKE_ROWS), dict(FAKE_META)

    monkeypatch.setattr(srv.fs, "search", fake_search)
    monkeypatch.setattr(srv, "index_fetched", lambda *a, **k: 0)
    c = TestClient(srv.app)

    assert c.post("/search", json={"query": "q"}).status_code == 200
    assert seen.get("language") is None


def test_the_openclaw_plugin_payload_is_accepted(client):
    """The exact body that plugin sends, field for field.

    Pinned as a whole rather than field by field because the failure was not
    any one field being wrong -- it was the combination a real caller sends
    going untested while each field looked defensible on its own.
    """
    body = {"query": "raspberry pi", "num_results": 5, "engines": "auto",
            "language": "en", "fresh": False, "profile": "fast"}
    assert client.post("/search", json=body).status_code == 200

    fetch_body = {"urls": ["https://example.com"], "max_chars": 3000,
                  "fresh": False, "profile": "fast"}
    assert client.post("/fetch", json=fetch_body).status_code == 200


def test_language_is_declared_because_it_is_honoured(client):
    """Declared fields are promises. This one is kept; engines/profile are not
    declared precisely because they are accepted and change nothing."""
    declared = client.get("/v2/capabilities").json()["optional_fields"]
    assert "language" in declared["search"]
    assert "engines" not in declared["search"]
    assert "profile" not in declared["search"]
