"""Three defects found by someone actually using the thing.

A reviewer ran dethrottled as an agent would and found what internal testing
had not: a 404 reported as a success, a mistyped URL blamed on robots.txt, and
an API reporting a version it had not been for two releases. All three are the
same failure in different clothes -- the software told the caller something
that was not true.

The fourth, and worst, is pinned here too: the external reader tier defaulted
to ON while the documentation said it was off. A tool whose claim is that
nothing leaves your network must not send every URL you fetch to a third party
unless you asked it to.
"""
import pathlib

import pytest

from dethrottled import __version__, server
from dethrottled import fetch as f

# ── the reader must be opt-in ────────────────────────────────────────────────

def test_the_external_reader_is_off_unless_asked(monkeypatch):
    """The one tier that leaves your network. Coverage you did not consent to
    is not a feature."""
    monkeypatch.delenv("DETHROTTLED_ENABLE_JINA", raising=False)
    import importlib
    reloaded = importlib.reload(f)
    try:
        assert reloaded.ENABLE_JINA is False
    finally:
        importlib.reload(f)


def test_the_reader_reports_itself_disabled_rather_than_failing():
    saved = f.ENABLE_JINA
    f.ENABLE_JINA = False
    try:
        _payload, reason, _final = f._tier_jina_reader("https://example.com/a")
        assert reason == "jina_reader_disabled"
    finally:
        f.ENABLE_JINA = saved


# ── an upstream error is not prose ───────────────────────────────────────────

@pytest.mark.parametrize("status", ["404", "403", "500"])
def test_reader_error_pages_are_not_accepted_as_content(status):
    """The reader answers 200 with the target's ERROR PAGE and says so in a
    warning line. That was being accepted as a successful extraction."""
    body = ("Title: Some Page\nURL Source: https://example.com/x\n"
            "Warning: Target URL returned error %s: Not Found\n"
            "Markdown Content:\n" + ("navigation chrome " * 80)) % status
    assert f._reader_reported_error(body) == status


def test_a_healthy_reader_response_is_left_alone():
    body = "Title: Real Article\nMarkdown Content:\n" + ("real prose " * 200)
    assert f._reader_reported_error(body) == ""


def test_an_article_discussing_errors_is_not_mistaken_for_one():
    """Only the reader's own warning header counts, and only near the top."""
    body = "Markdown Content:\n" + ("x " * 2000) + "Target URL returned error 404"
    assert f._reader_reported_error(body) == ""


# ── a mistyped URL is not a robots decision ──────────────────────────────────

@pytest.mark.parametrize("bad", [
    "not a url", "https://", "htp:/broken", "", "   ", "ftp://files.example/x",
    "javascript:alert(1)", "file:///etc/passwd",
])
def test_unfetchable_urls_are_rejected_as_invalid(bad):
    assert f.valid_url(bad) is False


@pytest.mark.parametrize("good", [
    "https://example.com/", "http://example.com/a?b=c",
    "https://sub.example.co.uk/path#frag",
])
def test_real_urls_pass(good):
    assert f.valid_url(good) is True


def test_a_malformed_url_says_so_rather_than_blaming_robots(monkeypatch):
    """robots_allows fails closed -- correct for a lookup that errors, and
    badly wrong as an answer to "that is not a URL". It sent people looking
    for a rule that did not exist."""
    called = []
    monkeypatch.setattr(f, "robots_allows",
                        lambda url, cache=None: called.append(url) or True)
    result = f.fetch_and_extract("not a url")
    assert result["ok"] is False
    assert result["reason"] == "invalid_url"
    assert called == [], "robots must not even be consulted"


# ── one version, not three ───────────────────────────────────────────────────

def test_the_api_reports_the_real_version():
    """server.py hardcoded 0.1.0 while the package said 0.1.2, so /health and
    /v2/capabilities advertised a version the software had not been for two
    releases -- and a reviewer duly wrote it up as early-stage."""
    assert server.VERSION == __version__


def test_the_version_is_declared_in_exactly_one_place():
    import re
    src = pathlib.Path(server.__file__).read_text(encoding="utf-8")
    assert not re.search(r'VERSION\s*=\s*[\'"]\d+\.\d+', src), \
        "the version must be imported, never restated"



def test_no_module_hardcodes_a_version_number():
    """The check above only guarded server.py, so the identical mistake sat in
    fetch.py the whole time: the User-Agent said "dethrottled/0.1" while the
    package was 0.2.0. Guard every module, not the one that was caught."""
    import re
    pkg = pathlib.Path(server.__file__).parent
    offenders = []
    for path in sorted(pkg.glob("*.py")):
        if path.name == "__init__.py":
            continue          # the one legitimate home for the number
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "version" not in code.lower() and "dethrottled/" not in code:
                continue
            if re.search(r'["\'][^"\']*\b\d+\.\d+\.?\d*', code):
                offenders.append("%s:%d %s" % (path.name, number, line.strip()))
    assert not offenders, (
        "version restated instead of imported:\n" + "\n".join(offenders))


def test_the_user_agent_is_actually_contactable():
    """It shipped with the templating placeholder still in it, so every request
    we made announced a URL that 404s. An unreachable contact address is not a
    contact address, and this is the string that asks to be told to stop."""
    assert "DETHROTTLED_GITHUB_USER" not in f.PROJECT_URL
    assert f.PROJECT_URL.startswith("https://github.com/")
    assert __version__ in f.USER_AGENT
