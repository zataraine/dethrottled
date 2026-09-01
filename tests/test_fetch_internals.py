"""fetch.py: the ladder, the budgets, robots, and the thin-result rule.

The central claim of this project is that escalation is driven by recovered
PROSE and not by HTTP status. That claim lives in `fetch_and_extract`, and until
this file existed it was asserted in a docstring and tested nowhere.

Every tier is stubbed. Nothing here touches the network, so a failure means
this code is wrong rather than that a publisher was having a bad morning.
"""
import pytest

from dethrottled import fetch as f

# All three fixtures must exceed _usable()'s 800-character floor, or the ladder
# discards them as stubs before extraction is ever attempted -- which would make
# these tests pass for entirely the wrong reason.
ARTICLE = ("<html><head><title>Real</title></head><body><article>"
           + "<p>%s</p>" % ("Sparse retrieval scores a document by how often a "
                            "query term appears, weighted by how rare that term "
                            "is across the whole collection. " * 12)
           + "</article></body></html>")

# Valid HTML, would arrive as HTTP 200, comfortably over the size floor -- and
# containing no article text at all. This is the exact shape the ladder exists
# to defeat, so it has to be big enough to look like a real page.
SHELL = ("<html><head><title>Loading</title></head><body>"
         "<div id='root'></div>"
         + "<script>window.__DATA__={%s};</script>" % ("'k':'v'," * 120)
         + "<nav>Home About Contact</nav><footer>Copyright</footer>"
         "</body></html>")

# Over the size floor, but its recoverable prose is under THIN_CHARS.
THIN = ("<html><head><title>Teaser</title></head><body>"
        + "<script>%s</script>" % ("var padding = 1; " * 60)
        + "<article><p>Only a sentence of teaser text here.</p></article>"
        "</body></html>")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No robots lookups, no domain throttle, no cooldown leakage."""
    monkeypatch.setattr(f, "robots_allows", lambda url, cache=None: True)
    monkeypatch.setattr(f, "_throttle", lambda domain: None)
    monkeypatch.setattr(f, "CRAWL4AI_URL", "http://renderer.invalid")
    monkeypatch.setattr(f, "ENABLE_JINA", False)
    f._tier_rest.clear()
    f.reset_tier_stats()
    yield
    f._tier_rest.clear()


def tier(html=None, text=None, reason=""):
    """A stubbed tier returning a fixed payload."""
    payload = {}
    if html is not None:
        payload["html"] = html
    if text is not None:
        payload["text"] = text
    return lambda url, *a, **k: (payload, reason, url)


# ── the central claim ────────────────────────────────────────────────────────

def test_http_200_with_no_prose_escalates(monkeypatch):
    """The finding the whole ladder is built on. `direct` returns a valid page
    and zero article text; the run must NOT stop there."""
    monkeypatch.setattr(f, "_tier_direct", tier(html=SHELL))
    monkeypatch.setattr(f, "_tier_tls", tier(html=ARTICLE))
    result = f.fetch_and_extract("https://example.com/a", max_chars=5000)
    assert result["ok"]
    assert result["tier"] == "tls", "must have escalated past the empty shell"


def test_thin_prose_also_escalates(monkeypatch):
    """Technically prose, too little to be worth stopping on."""
    monkeypatch.setattr(f, "_tier_direct", tier(html=THIN))
    monkeypatch.setattr(f, "_tier_tls", tier(html=ARTICLE))
    result = f.fetch_and_extract("https://example.com/a", max_chars=5000)
    assert result["tier"] == "tls"


def test_a_good_first_tier_stops_the_ladder(monkeypatch):
    """The cheap tier solves most pages; escalating anyway would waste them."""
    called = []
    monkeypatch.setattr(f, "_tier_direct", tier(html=ARTICLE))
    monkeypatch.setattr(f, "_tier_tls",
                        lambda url, *a, **k: (called.append("tls"), ({}, "", url))[1])
    result = f.fetch_and_extract("https://example.com/a", max_chars=5000)
    assert result["tier"] == "direct"
    assert called == [], "must not have tried a later tier"


def test_native_text_skips_local_extraction(monkeypatch):
    """A renderer's own extraction beats re-extracting from its HTML; running a
    local extractor over it only loses content."""
    monkeypatch.setattr(f, "_tier_direct", tier(html=SHELL))
    monkeypatch.setattr(f, "_tier_tls", tier(html=SHELL))
    monkeypatch.setattr(f, "_tier_crawl4ai",
                        tier(text="Clean markdown prose. " * 30))
    result = f.fetch_and_extract("https://example.com/a", max_chars=5000)
    assert result["ok"]
    assert result["extractor"] == "crawl4ai-native"


def test_everything_failing_reports_every_reason(monkeypatch):
    """A failure that says only "failed" cannot be diagnosed."""
    monkeypatch.setattr(f, "_tier_direct", tier(html=SHELL))
    monkeypatch.setattr(f, "_tier_tls", tier(reason="tls_http_403"))
    monkeypatch.setattr(f, "_tier_crawl4ai", tier(reason="crawl4ai_empty"))
    result = f.fetch_and_extract("https://example.com/a", max_chars=5000)
    assert result["ok"] is False
    assert "tls" in result["reason"] and "crawl4ai" in result["reason"]


# ── robots ───────────────────────────────────────────────────────────────────

def test_robots_disallow_stops_everything(monkeypatch):
    """A relay is a different route to the same publisher, not permission."""
    monkeypatch.setattr(f, "robots_allows", lambda url, cache=None: False)
    tried = []
    monkeypatch.setattr(f, "_tier_direct",
                        lambda *a, **k: (tried.append("direct"), ({}, "", "u"))[1])
    result = f.fetch_and_extract("https://example.com/a")
    assert result["ok"] is False
    assert result["reason"] == "robots_disallow"
    assert tried == []


# ── budgets ──────────────────────────────────────────────────────────────────

def test_tier_budget_is_spent_and_refuses():
    assert f._spend("probe", 2) is True
    assert f._spend("probe", 2) is True
    assert f._spend("probe", 2) is False, "third call must exceed a budget of 2"


def test_budget_state_is_visible():
    f._spend("probe2", 5)
    assert f.budget_state().get("probe2", 0) >= 1


def test_page_budget_stops_the_next_tier(monkeypatch):
    """Checked BETWEEN tiers: a tier already running is allowed to finish,
    because killing a fetch about to succeed wastes everything spent on it."""
    import time
    monkeypatch.setattr(f, "_tier_direct",
                        lambda *a, **k: (time.sleep(0.15), ({"html": SHELL}, "", "u"))[1])
    reached = []
    monkeypatch.setattr(f, "_tier_tls",
                        lambda url, *a, **k: (reached.append("tls"),
                                              ({"html": ARTICLE}, "", url))[1])
    result = f.fetch_and_extract("https://example.com/a", page_budget=0.01)
    assert reached == [], "budget was spent; the next tier must be skipped"
    assert result["ok"] is False


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("html,ok", [
    ("", False),
    ("<html></html>", False),
    ("x" * 50, False),
    ("<html><body>" + "y" * 900 + "</body></html>", True),
])
def test_usable_rejects_stubs(html, ok):
    assert f._usable(html) is ok


def test_tidy_text_collapses_whitespace():
    assert f._tidy_text("a   b\n\n c\t d") == "a b c d"


def test_tidy_text_respects_a_limit():
    assert len(f._tidy_text("word " * 500, limit=40)) <= 40


def test_clip_returns_what_was_asked_for():
    out = f._clip({"ok": True, "text": "x" * 900, "chars": 900}, 100)
    assert len(out["text"]) == 100


# ── cooldown interaction ─────────────────────────────────────────────────────

def test_a_resting_tier_is_skipped_entirely(monkeypatch):
    monkeypatch.setattr(f, "_tier_direct", tier(html=SHELL))
    monkeypatch.setattr(f, "_tier_tls", tier(html=SHELL))
    asked = []
    monkeypatch.setattr(f, "_tier_crawl4ai",
                        lambda url, *a, **k: (asked.append(1), ({}, "", url))[1])
    f.tier_refused("crawl4ai", "crawl4ai_http_429")
    result = f.fetch_and_extract("https://example.com/a")
    assert asked == [], "a resting tier must not be asked"
    assert "resting" in result["reason"]
