"""health.py: the states, and the rule against crying wolf.

The module exists to catch silent degradation, so the states it reports have to
be exactly right. Two distinctions carry all the value:

    DEGRADED vs OK    a tier that answers with no usable prose is NOT working,
                      and this is the state that hid two multi-day outages
    OFF vs DEAD       a tier nobody configured has not failed, and reporting it
                      as failed makes a healthy minimal install look broken

Everything is stubbed; no probe here touches the network.
"""
import pytest

from dethrottled import health as h

ARTICLE = ("<html><head><title>T</title></head><body><article>"
           + "<p>%s</p>" % ("Sparse retrieval scores a document by how often a "
                            "query term appears, weighted by rarity. " * 14)
           + "</article></body></html>")

SHELL = ("<html><head><title>Loading</title></head><body><div id='root'></div>"
         + "<script>window.__D={%s};</script>" % ("'k':'v'," * 140)
         + "</body></html>")


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """Everything switched on unless a test says otherwise."""
    monkeypatch.setattr(h.fetcher, "CRAWL4AI_URL", "http://renderer.invalid")
    monkeypatch.setattr(h.fetcher, "ENABLE_JINA", True)
    monkeypatch.setattr(h.fetcher, "ENABLE_TLS", True)
    yield


# ── tier states ──────────────────────────────────────────────────────────────

def test_prose_is_ok(monkeypatch):
    monkeypatch.setattr(h.fetcher, "_tier_direct",
                        lambda *a, **k: ({"html": ARTICLE}, "", "https://x/"))
    assert h.probe_tier("direct", "https://x/")["status"] == "OK"


def test_html_with_no_prose_is_degraded_not_ok(monkeypatch):
    """The whole reason this module exists. HTTP 200, a real body, no article."""
    monkeypatch.setattr(h.fetcher, "_tier_direct",
                        lambda *a, **k: ({"html": SHELL}, "", "https://x/"))
    row = h.probe_tier("direct", "https://x/")
    assert row["status"] == "DEGRADED"
    assert "no prose" in row["detail"]


def test_short_prose_is_thin(monkeypatch):
    thin = ("<html><body><article><p>%s</p></article></body></html>"
            % ("Short teaser. " * 25))
    monkeypatch.setattr(h.fetcher, "_tier_direct",
                        lambda *a, **k: ({"html": thin}, "", "https://x/"))
    assert h.probe_tier("direct", "https://x/")["status"] in ("THIN", "DEGRADED")


def test_nothing_at_all_is_dead(monkeypatch):
    monkeypatch.setattr(h.fetcher, "_tier_direct",
                        lambda *a, **k: ({}, "connection_refused", "https://x/"))
    row = h.probe_tier("direct", "https://x/")
    assert row["status"] == "DEAD"
    assert row["detail"] == "connection_refused"


def test_an_exception_is_dead_not_a_crash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(h.fetcher, "_tier_direct", boom)
    assert h.probe_tier("direct", "https://x/")["status"] == "DEAD"


def test_native_text_is_accepted_without_extraction(monkeypatch):
    monkeypatch.setattr(h.fetcher, "_tier_crawl4ai",
                        lambda *a, **k: ({"text": "Clean prose. " * 60}, "", "https://x/"))
    row = h.probe_tier("crawl4ai", "https://x/")
    assert row["status"] == "OK"
    assert row["detail"] == "native extraction"


# ── OFF, and why it matters ──────────────────────────────────────────────────

def test_unconfigured_renderer_is_off_not_dead(monkeypatch):
    monkeypatch.setattr(h.fetcher, "CRAWL4AI_URL", "")
    assert h.probe_tier("crawl4ai", "https://x/")["status"] == "OFF"


def test_disabled_jina_is_off_not_dead(monkeypatch):
    monkeypatch.setattr(h.fetcher, "ENABLE_JINA", False)
    assert h.probe_tier("jina-reader", "https://x/")["status"] == "OFF"


def test_a_tier_declining_politely_is_off(monkeypatch):
    """A resting or budget-exhausted tier is not a fault, and the detail should
    send the reader to the setting rather than to a debugger."""
    monkeypatch.setattr(h.fetcher, "_tier_crawl4ai",
                        lambda *a, **k: ({}, "crawl4ai_budget_spent", "https://x/"))
    assert h.probe_tier("crawl4ai", "https://x/")["status"] == "OFF"


# ── the verdict ──────────────────────────────────────────────────────────────

def ok_tier(name="direct"):
    return {"tier": name, "status": "OK", "chars": 5000, "detail": "", "ms": 1}


def off_tier(name="crawl4ai"):
    return {"tier": name, "status": "OFF", "chars": 0, "detail": "", "ms": 0}


def dead_tier(name="tls"):
    return {"tier": name, "status": "DEAD", "chars": 0, "detail": "", "ms": 1}


def src(status, name="web"):
    return {"source": name, "status": status, "count": 1, "detail": "", "ms": 1}


def test_a_minimal_install_is_healthy():
    """One tier working and one search source working, everything else OFF.
    This is a plain `pip install` and it must exit 0."""
    code, summary = h.verdict([ok_tier(), off_tier(), off_tier("jina-reader")],
                              [src("OK"), src("OFF", "searxng")])
    assert code == 0, summary


def test_one_dead_tier_is_survivable():
    """The ladder has others; this is degraded, not critical."""
    code, _ = h.verdict([ok_tier(), dead_tier()], [src("OK")])
    assert code == 1


def test_losing_every_search_source_is_critical():
    """Nothing downstream has anything to work on."""
    code, summary = h.verdict([ok_tier()], [src("DEAD"), src("DEAD", "bing-news")])
    assert code == 2
    assert "search" in summary


def test_off_sources_do_not_count_as_dead():
    """An unconfigured SearXNG must not make a working stack look critical."""
    code, _ = h.verdict([ok_tier()], [src("OK"), src("OFF", "searxng")])
    assert code == 0


def test_no_working_tier_is_critical():
    code, summary = h.verdict([dead_tier(), off_tier()], [src("OK")])
    assert code == 2
    assert "fetch tier" in summary


def test_degraded_is_reported_but_not_critical():
    code, _ = h.verdict([ok_tier(), {"tier": "tls", "status": "DEGRADED",
                                     "chars": 0, "detail": "", "ms": 1}],
                        [src("OK")])
    assert code == 1


# ── no leftovers from the project this came from ─────────────────────────────

def test_probes_are_generic():
    """The search probe once hardcoded a semiconductor supply-chain query. A
    health check must not be tuned to one deployment's subject matter."""
    import inspect
    source = inspect.getsource(h)
    for leftover in ("TSMC", "CoWoS", "semiconductor"):
        assert leftover not in source


def test_every_ladder_tier_has_a_probe():
    """A tier the health check cannot see is a tier that can fail silently --
    which is the exact failure this module was written for."""
    assert {"direct", "tls", "crawl4ai"} <= set(h.PROBES)
