"""The extraction cascade, and the empty-shell case that shapes the ladder.

The last test here is the one that matters. The whole fetch ladder is built
around the finding that HTTP 200 is not success -- a page can return a
perfectly valid response containing no article text whatsoever, and a stack
that treats status codes as truth declares victory on it. These tests pin the
behaviour that makes escalation work.
"""
from dethrottled import extract as fx

ARTICLE = """<!doctype html>
<html><head><title>A Real Article</title></head><body>
<nav>Home About Contact Subscribe</nav>
<article>
<h1>A Real Article</h1>
<p>Sparse retrieval scores a document by how often a query term appears in it,
weighted by how rare that term is across the whole collection. The rarer the
term, the more its presence tells you. This is the intuition behind every
term-weighting scheme in common use, and it is why a bag of words remains
competitive with far more expensive methods on short queries.</p>
<p>The length normalisation matters as much as the weighting. Without it a long
document wins simply by containing more words, which is not the same thing as
being more relevant to the question that was asked.</p>
</article>
<footer>Copyright 2026. Cookie notice. Related articles.</footer>
</body></html>"""

# HTTP 200, valid HTML, a title, a full navigation chrome -- and not one word of
# article text. This is what a JavaScript-rendered page serves to a plain
# fetcher, and it is the exact shape that defeats a status-code-driven ladder.
EMPTY_SHELL = """<!doctype html>
<html><head><title>Loading...</title></head><body>
<nav>Home About Contact</nav>
<div id="root"></div>
<footer>Copyright 2026</footer>
<script>window.__DATA__={};</script>
</body></html>"""


def test_extracts_the_article_body():
    result = fx.extract(ARTICLE, url="https://example.com/a", max_chars=5000)
    assert result["ok"]
    assert "Sparse retrieval scores a document" in result["text"]


def test_drops_the_chrome():
    """Navigation and footer vote in every ranking decision if they survive."""
    result = fx.extract(ARTICLE, url="https://example.com/a", max_chars=5000)
    assert "Cookie notice" not in result["text"]
    assert "Subscribe" not in result["text"]


def test_recovers_the_title():
    result = fx.extract(ARTICLE, url="https://example.com/a", max_chars=5000)
    assert "Real Article" in (result.get("title") or "")


def test_max_chars_is_respected():
    result = fx.extract(ARTICLE, url="https://example.com/a", max_chars=120)
    assert len(result["text"]) <= 120


def test_an_empty_shell_is_not_a_success():
    """The finding the whole tier ladder is built on.

    This document is valid HTML and would arrive with HTTP 200. If extraction
    reported success here, the fetcher would stop and return a page containing
    no article text -- so escalation must be driven by recovered prose, never
    by the status code.
    """
    result = fx.extract(EMPTY_SHELL, url="https://example.com/a", max_chars=5000)
    assert not result["ok"] or len(result["text"]) < 220


def test_empty_input_is_not_fatal():
    assert not fx.extract("", url="https://example.com/a", max_chars=100)["ok"]


def test_available_reports_the_installed_extractors():
    """selectolax is the floor of the cascade, so it is a hard dependency: if
    the last resort can be missing, a trafilatura failure becomes a failed
    extraction rather than a scruffy one."""
    have = fx.available()
    assert have["selectolax"] is True
    assert set(have) == {"trafilatura", "resiliparse", "selectolax"}


def test_beautifulsoup_is_gone():
    """It was the worst rung on both axes at once -- 37ms and 66% boilerplate,
    slower AND dirtier than everything that replaced it. This pins the removal
    so it cannot creep back in as a convenience import."""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(fx.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # The docstring still NAMES BeautifulSoup, because recording why a thing
    # was removed is worth more than pretending it never existed. What must not
    # come back is the import.
    assert "bs4" not in imported
    assert "readability" not in imported
