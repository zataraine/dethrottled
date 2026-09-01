"""Domain fetchability: learned, decaying, and narrow in what it may do.

The danger with a mechanism like this is not that it fails to work -- it is
that it works too eagerly and quietly starts hiding half the web. So the tests
below spend most of their effort on what it must NOT do: not act on thin
evidence, not reorder relevance, not drop anything, and not hold a grudge.
"""
import pytest

from dethrottled import domains as d


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("DETHROTTLED_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(d, "_state", {})
    monkeypatch.setattr(d, "ENABLED", True)
    yield
    monkeypatch.setattr(d, "_state", {})


def fail(url, n):
    for _ in range(n):
        d.record(url, False)


def succeed(url, n):
    for _ in range(n):
        d.record(url, True)


# ── the domain key ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.example.com/a", "example.com"),
    ("https://example.com/a", "example.com"),
    ("https://EXAMPLE.com:8443/a", "example.com"),
    ("https://sub.example.com/a", "sub.example.com"),
])
def test_domain_key(url, expected):
    assert d.domain_of(url) == expected


def test_rubbish_urls_do_not_raise():
    assert d.domain_of("") == ""
    assert d.domain_of("not a url") == ""
    assert d.record("nonsense", False) is None


# ── restraint ────────────────────────────────────────────────────────────────

def test_unknown_domains_are_not_suspicious():
    """An unseen site is unseen, not bad. Scoring it 0 would make the very
    first fetch of every new domain a deprioritised one."""
    assert d.score("https://never-seen.example/a") == 0.5
    assert d.is_poor("https://never-seen.example/a") is False


def test_one_bad_day_is_not_a_verdict():
    """Below the attempt threshold nothing is acted on, however bad it looks."""
    fail("https://flaky.example/a", d.MIN_ATTEMPTS - 1)
    assert d.is_poor("https://flaky.example/a") is False


def test_sustained_failure_is_acted_on():
    fail("https://spa.example/a", d.MIN_ATTEMPTS + 3)
    assert d.is_poor("https://spa.example/a") is True
    assert d.score("https://spa.example/a") < d.FLOOR


def test_a_working_site_is_never_poor():
    succeed("https://good.example/a", 20)
    assert d.is_poor("https://good.example/a") is False
    assert d.score("https://good.example/a") > 0.9


def test_mostly_working_sites_survive_occasional_failures():
    """Real sites have bad pages. Eight successes and two failures is a healthy
    domain, not a broken one."""
    succeed("https://normal.example/a", 8)
    fail("https://normal.example/a", 2)
    assert d.is_poor("https://normal.example/a") is False


# ── recovery ─────────────────────────────────────────────────────────────────

def test_evidence_decays_so_a_site_can_recover(monkeypatch):
    """A site broken in March must not be condemned in June."""
    fail("https://fixed.example/a", 20)
    assert d.is_poor("https://fixed.example/a") is True

    import time
    entry = d._state["fixed.example"]
    entry["ts"] = time.time() - d.HALF_LIFE_DAYS * 86400 * 10   # ten half-lives
    assert d.is_poor("https://fixed.example/a") is False


def test_success_after_failure_pulls_the_score_back_up():
    fail("https://recovering.example/a", 10)
    assert d.is_poor("https://recovering.example/a") is True
    succeed("https://recovering.example/a", 40)
    assert d.is_poor("https://recovering.example/a") is False


# ── what it may do to results ────────────────────────────────────────────────

def rows(*urls):
    return [{"url": u, "title": u} for u in urls]


def test_ordering_moves_poor_domains_last_and_drops_nothing():
    fail("https://bad.example/x", 20)
    pool = rows("https://bad.example/1", "https://good.example/2",
                "https://other.example/3")
    out = d.order_for_fetching(pool)
    assert len(out) == len(pool), "must never drop a result"
    assert out[-1]["url"] == "https://bad.example/1"
    assert {r["url"] for r in out} == {r["url"] for r in pool}


def test_relative_order_of_healthy_rows_is_untouched():
    """It must not become a second ranker. Among domains it has no complaint
    about, the order it was given is the order it returns."""
    fail("https://bad.example/x", 20)
    pool = rows("https://a.example/1", "https://b.example/2",
                "https://bad.example/3", "https://c.example/4")
    out = d.order_for_fetching(pool)
    healthy = [r["url"] for r in out if "bad.example" not in r["url"]]
    assert healthy == ["https://a.example/1", "https://b.example/2",
                       "https://c.example/4"]


def test_an_all_poor_pool_is_returned_intact():
    """Deprioritising everything is the same as deprioritising nothing, and the
    caller still gets their results."""
    fail("https://bad.example/x", 20)
    pool = rows("https://bad.example/1", "https://bad.example/2")
    assert len(d.order_for_fetching(pool)) == 2


def test_disabled_is_a_complete_no_op(monkeypatch):
    monkeypatch.setattr(d, "ENABLED", False)
    fail("https://bad.example/x", 50)
    pool = rows("https://bad.example/1", "https://good.example/2")
    assert d.order_for_fetching(pool) == pool
    assert d.is_poor("https://bad.example/1") is False
    assert d.score("https://bad.example/1") == 1.0


# ── visibility and persistence ───────────────────────────────────────────────

def test_stats_shows_what_was_decided():
    """A system that quietly deprioritises part of the web must be able to say
    exactly what it decided and on what evidence."""
    fail("https://bad.example/x", 20)
    succeed("https://good.example/x", 10)
    report = d.stats()
    assert report["enabled"] is True
    assert report["domains"]["bad.example"]["deprioritised"] is True
    assert report["domains"]["good.example"]["deprioritised"] is False


def test_state_survives_a_reload(tmp_path):
    fail("https://bad.example/x", 20)
    d.flush()
    d._state = None                       # force a read from disk
    assert d.is_poor("https://bad.example/1") is True
