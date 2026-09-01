"""search.py's own logic: deduplication, banding, engine resting.

This module was the least-tested part of the stack and it is the one that
decides what the rest of the pipeline ever sees. Nothing here touches the
network -- every function below is pure, or reads a file this test owns.
"""
import pathlib
import time

import pytest

from dethrottled import search as fs


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Engine health is a file on disk; give each test its own."""
    monkeypatch.setenv("DETHROTTLED_ENGINE_HEALTH", str(tmp_path / "engines.json"))
    yield


# ── domain and title keys ────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.example.com/a", "example.com"),
    ("https://example.com/a", "example.com"),
    ("https://sub.example.com/a", "sub.example.com"),
    ("not a url", ""),
    ("", ""),
])
def test_domain(url, expected):
    assert fs._domain(url) == expected


def test_title_key_ignores_punctuation_and_case():
    a = fs._title_key("TSMC's CoWoS capacity -- lead times ease")
    b = fs._title_key("TSMC s CoWoS capacity  lead times ease!")
    assert a == b


def test_title_key_drops_short_words():
    """Two-letter words carry no signal and would make unrelated headlines
    collide."""
    assert "of" not in fs._title_key("The end of an era").split()


# ── the junk band ────────────────────────────────────────────────────────────

def rows(*urls):
    return [{"url": u, "title": u} for u in urls]


def test_preferred_domains_come_first():
    pool = rows("https://random.example/a", "https://reuters.com/b")
    out = fs.rank(pool, prefer={"reuters.com"})
    assert out[0]["url"] == "https://reuters.com/b"


def test_junk_domains_go_last():
    junk = next(iter(fs.JUNK_DOMAINS))
    pool = rows("https://%s/a" % junk, "https://normal.example/b")
    assert fs.rank(pool)[-1]["url"] == "https://%s/a" % junk


def test_rank_preserves_order_within_a_band():
    """Banding must not become a second ranking pass: inside a band the order
    it was given is the order it returns."""
    pool = rows("https://a.example/1", "https://b.example/2", "https://c.example/3")
    assert [r["url"] for r in fs.rank(pool)] == [r["url"] for r in pool]


def test_rank_never_drops_rows():
    junk = next(iter(fs.JUNK_DOMAINS))
    pool = rows("https://%s/a" % junk, "https://x.example/b", "https://reuters.com/c")
    assert len(fs.rank(pool, prefer={"reuters.com"})) == 3


def test_subdomain_of_a_preferred_domain_counts():
    pool = rows("https://other.example/a", "https://feeds.reuters.com/b")
    assert fs.rank(pool, prefer={"reuters.com"})[0]["url"].endswith("/b")


# ── engine resting ───────────────────────────────────────────────────────────

def test_a_failing_engine_is_rested():
    fs._record_failure("mojeek", "blocked")
    assert "mojeek" not in fs._rested("bing,mojeek").split(",")


def test_resting_expires():
    fs._record_failure("mojeek", "blocked")
    health = fs._load_health()
    health["mojeek"]["at"] = time.time() - (fs._HEALTH_TTL + 60)
    fs._save_health(health)
    assert "mojeek" in fs._rested("bing,mojeek").split(",")


def test_the_last_engine_standing_is_never_rested():
    """A wrong health record must not be able to turn every search into an
    empty result."""
    fs._record_failure("bing", "blocked")
    assert fs._rested("bing") == "bing"


def test_failures_accumulate_a_count():
    fs._record_failure("bing", "one")
    fs._record_failure("bing", "two")
    assert fs._load_health()["bing"]["fails"] == 2


def test_a_failed_health_write_is_not_fatal(monkeypatch, tmp_path):
    """Health is an optimisation. Losing it must not take searching with it.

    The failure is injected at the write rather than by choosing an unwritable
    path, because what counts as unwritable differs by platform -- and the
    promise being made here is about not propagating the error, not about which
    paths a given OS rejects.
    """
    def refuse(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(pathlib.Path, "write_text", refuse)
    fs._save_health({"a": {"at": 1}})          # must not raise


def test_an_unreadable_health_file_reads_as_empty(tmp_path, monkeypatch):
    """Corrupt or truncated JSON is a lost optimisation, not an error."""
    path = tmp_path / "engines.json"
    path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("DETHROTTLED_ENGINE_HEALTH", str(path))
    assert fs._load_health() == {}


def test_health_survives_a_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("DETHROTTLED_ENGINE_HEALTH",
                       str(tmp_path / "sub" / "engines.json"))
    fs._save_health({"a": {"at": 1}})          # parent directory is created
    assert fs._load_health() == {"a": {"at": 1}}


# ── bing's redirector ────────────────────────────────────────────────────────

def test_unwrap_bing_recovers_the_publisher_url():
    """The whole reason Bing News is the primary source: the real URL is in the
    redirect and needs no extra request."""
    wrapped = ("https://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&"
               "url=https%3A%2F%2Fexample.com%2Fstory&c=1")
    assert fs._unwrap_bing(wrapped) == "https://example.com/story"


def test_unwrap_bing_leaves_a_plain_url_alone():
    assert fs._unwrap_bing("https://example.com/a") == "https://example.com/a"


# ── guards ───────────────────────────────────────────────────────────────────

def test_searxng_is_skipped_when_unconfigured(monkeypatch):
    """Unset, it must not attempt a request to the empty string."""
    monkeypatch.setattr(fs, "SEARXNG_URL", "")
    assert fs.searxng("anything") == []


def test_web_search_without_ddgs_is_empty_not_fatal(monkeypatch):
    """ddgs is an optional dependency; absent, the RSS sources carry search."""
    import builtins
    real_import = builtins.__import__

    def no_ddgs(name, *a, **k):
        if name == "ddgs":
            raise ImportError("no ddgs")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_ddgs)
    assert fs.web_search("anything") == []
