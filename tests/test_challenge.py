"""Telling an interactive challenge apart from a refusal.

Both arrive as a 403 and they mean opposite things. "Forbidden" says stop
asking. A managed challenge says the site is willing to serve this page to
anyone who can run its JavaScript and, at the end of it, tick a box.

Conflating them cost real time: four sites were recorded as flat 403s and
written up as IP reputation, and the ranked hypotheses that followed -- header
order, HTTP/2 fingerprinting, JA4 -- were chasing a pre-challenge gap that did
not exist. Every one of those responses was in fact a Cloudflare challenge
document, and a real Chrome on the same address received the identical 403.

So this is not a thing to defeat. It is a thing to report accurately.
"""
import pytest

from dethrottled import fetch as f

CHALLENGE_BODY = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    "<body><div class='main-wrapper'>"
    "<h1>www.example.com</h1><p>Verifying you are human. This may take a few "
    "seconds.</p><script src='/cdn-cgi/challenge-platform/h/b/orchestrate/"
    "chl_page/v1'></script></div></body></html>"
)

REAL_FORBIDDEN = (
    "<!DOCTYPE html><html><head><title>403 Forbidden</title></head>"
    "<body><h1>Forbidden</h1><p>You do not have permission to access this "
    "resource on this server.</p></body></html>"
)


# ── the header is authoritative ─────────────────────────────────────────────

def test_cf_mitigated_header_is_believed():
    """When the vendor labels it, take the label -- no body scan needed."""
    assert f._is_challenge(403, {"cf-mitigated": "challenge"}, "") is True


def test_cf_mitigated_is_case_insensitive():
    assert f._is_challenge(403, {"cf-mitigated": "CHALLENGE"}, "") is True


def test_cf_mitigated_beats_a_status_we_would_not_scan():
    """A challenge served on an unusual status is still a challenge."""
    assert f._is_challenge(200, {"cf-mitigated": "challenge"}, "") is True


# ── falling back to the body ────────────────────────────────────────────────

@pytest.mark.parametrize("status", [403, 429, 503])
def test_challenge_body_is_recognised_without_the_header(status):
    assert f._is_challenge(status, {}, CHALLENGE_BODY) is True


def test_a_real_forbidden_is_not_a_challenge():
    """The distinction that matters. This one means stop asking."""
    assert f._is_challenge(403, {}, REAL_FORBIDDEN) is False


def test_an_ordinary_404_is_not_a_challenge():
    assert f._is_challenge(404, {}, "<html><body>Not found</body></html>") is False


def test_a_page_merely_discussing_challenges_is_not_one():
    """A 200 that happens to contain the words must not be misread."""
    article = ("<html><body><p>Cloudflare's challenge-platform shows a "
               "Just a moment page while verifying you are human.</p></body></html>")
    assert f._is_challenge(200, {}, article) is False


def test_only_the_head_of_the_body_is_scanned():
    """A challenge document is small; scanning a whole page would be waste."""
    buried = "x" * 20000 + "just a moment"
    assert f._is_challenge(403, {}, buried) is False


def test_missing_headers_are_survivable():
    assert f._is_challenge(403, None, CHALLENGE_BODY) is True
    assert f._is_challenge(403, {}, None) is False


# ── the reason string callers actually see ──────────────────────────────────

def test_reason_names_a_challenge_plainly():
    reason = f._http_reason("", 403, {"cf-mitigated": "challenge"}, "")
    assert reason == "challenge_needs_a_human"


def test_reason_keeps_the_status_for_a_real_refusal():
    assert f._http_reason("", 403, {}, REAL_FORBIDDEN) == "http_403"
    assert f._http_reason("", 404, {}, "") == "http_404"


def test_the_prefix_identifies_the_tier():
    assert f._http_reason("tls_", 403, {}, REAL_FORBIDDEN) == "tls_http_403"
    assert f._http_reason("tls_", 403, {"cf-mitigated": "challenge"},
                          "") == "tls_challenge_needs_a_human"


# ── how the ladder treats it ────────────────────────────────────────────────

def test_a_challenge_counts_as_a_refusal_for_cooldown():
    """A tier that got a challenge did not get the page, so for a shared
    service it is still worth standing back from."""
    assert f._is_refusal("crawl4ai_http_403") is True


def test_a_challenge_does_not_rest_a_per_host_tier():
    """direct and tls are per-host operations. One site challenging us says
    nothing about the next site, and resting them globally once dropped
    end-to-end extraction from 87% to 67%."""
    f._tier_rest.clear()
    try:
        f.tier_refused("direct", "challenge_needs_a_human")
        f.tier_refused("tls", "tls_challenge_needs_a_human")
        assert f.tier_resting("direct") is False
        assert f.tier_resting("tls") is False
    finally:
        f._tier_rest.clear()
