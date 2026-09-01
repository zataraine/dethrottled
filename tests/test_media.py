"""Video URLs, routed to their captions.

A video page is the one thing the fetch ladder cannot win: fetching returns a
player, rendering returns a player, and no extractor gets prose from either.
The words are in a caption track served separately, so the decision is made
from the URL before any network is spent.

That makes URL recognition the whole risk. Too loose and every youtube.com
link -- a channel, a search, the home page -- becomes a transcript lookup that
can only fail, and the ladder never gets its chance. These tests are mostly
about the boundary.

Nothing here needs the network except the tests marked `network`.
"""
import pytest

from dethrottled import media

VIDEO = "dQw4w9WgXcQ"


# ── what counts as a video ───────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=" + VIDEO,
    "https://youtube.com/watch?v=" + VIDEO,
    "https://m.youtube.com/watch?v=" + VIDEO,
    "https://music.youtube.com/watch?v=" + VIDEO,
    "https://youtu.be/" + VIDEO,
    "https://www.youtube.com/embed/" + VIDEO,
    "https://www.youtube.com/shorts/" + VIDEO,
    "https://www.youtube.com/live/" + VIDEO,
    "https://www.youtube.com/v/" + VIDEO,
    "https://www.youtube.com/watch?v=%s&t=42s" % VIDEO,
])
def test_video_urls_are_recognised(url):
    assert media.video_id(url) == VIDEO
    assert media.is_video(url) is True


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/",
    "https://www.youtube.com/@somechannel",
    "https://www.youtube.com/results?search_query=bm25",
    "https://www.youtube.com/feed/subscriptions",
    "https://www.youtube.com/watch",                       # no id
    "https://www.youtube.com/watch?v=tooshort",
    "https://www.youtube.com/watch?v=waaaaaaaaaytoolong",
    "https://en.wikipedia.org/wiki/YouTube",
    "https://notyoutube.com/watch?v=" + VIDEO,
    "https://evil.example/youtube.com/watch?v=" + VIDEO,
    "",
    "not a url at all",
])
def test_non_video_urls_fall_through_to_the_ladder(url):
    """Anything not a video must cost nothing and take the normal path."""
    assert media.is_video(url) is False


def test_a_lookalike_host_is_not_youtube():
    """The host is checked against an allowlist, not matched as a substring."""
    assert media.is_video("https://youtube.com.evil.example/watch?v=" + VIDEO) is False


# ── failure reporting ────────────────────────────────────────────────────────

def test_disabled_is_reported_by_name(monkeypatch):
    monkeypatch.setattr(media, "ENABLED", False)
    text, _title, why = media.transcript("https://youtu.be/" + VIDEO)
    assert text == ""
    assert why == "transcripts_disabled"


def test_a_non_video_url_says_so():
    text, _title, why = media.transcript("https://example.com/a")
    assert text == ""
    assert why == "not_a_video_url"


def test_a_missing_library_is_reported_not_raised(monkeypatch):
    """The dependency is optional; absent, this must name itself rather than
    blow up a fetch."""
    import builtins
    real = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("youtube_transcript_api"):
            raise ImportError("absent")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    text, _title, why = media.transcript("https://youtu.be/" + VIDEO)
    assert text == ""
    assert why == "transcript_api_not_installed"


@pytest.mark.parametrize("exc_name,expected", [
    ("IpBlocked", "transcript_blocked_from_this_address"),
    ("RequestBlocked", "transcript_blocked_from_this_address"),
    ("NoTranscriptFound", "no_transcript_available"),
    ("TranscriptsDisabled", "no_transcript_available"),
    ("VideoUnavailable", "video_unavailable"),
])
def test_failures_are_told_apart(monkeypatch, exc_name, expected):
    """"No captions exist" and "this address is blocked" call for completely
    different responses from whoever reads the logs."""
    import sys
    import types
    fake = types.ModuleType("youtube_transcript_api")

    class Api:
        def fetch(self, *a, **k):
            raise type(exc_name, (Exception,), {})("nope")

    fake.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", fake)
    text, _title, why = media.transcript("https://youtu.be/" + VIDEO)
    assert text == ""
    assert why == expected


# ── shaping the captions ─────────────────────────────────────────────────────

def fake_api(monkeypatch, snippets):
    import sys
    import types
    fake = types.ModuleType("youtube_transcript_api")

    class Snippet:
        def __init__(self, text):
            self.text = text

    class Api:
        def fetch(self, *a, **k):
            return [Snippet(x) for x in snippets]

    fake.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", fake)


def test_fragments_become_continuous_prose(monkeypatch):
    """Captions arrive as timed fragments, often mid-sentence. Every consumer
    of this -- extractor, embedder, reader -- wants sentences, not a subtitle
    file."""
    fake_api(monkeypatch, ["Sparse retrieval scores", "documents by term",
                           "frequency across a collection."])
    text, _title, why = media.transcript("https://youtu.be/" + VIDEO)
    assert why == ""
    assert text == ("Sparse retrieval scores documents by term "
                    "frequency across a collection.")


def test_sound_cues_are_dropped(monkeypatch):
    """"[Music]" is a description of a sound, not something anybody said."""
    fake_api(monkeypatch, ["[Music]", "the actual words", "[Applause]"])
    text, _title, _why = media.transcript("https://youtu.be/" + VIDEO)
    assert text == "the actual words"


def test_the_limit_is_respected(monkeypatch):
    fake_api(monkeypatch, ["a longer fragment of speech here"] * 200)
    text, _title, _why = media.transcript("https://youtu.be/" + VIDEO, limit=100)
    assert len(text) <= 100


def test_captions_of_only_sound_cues_report_empty(monkeypatch):
    fake_api(monkeypatch, ["[Music]", "[Applause]"])
    text, _title, why = media.transcript("https://youtu.be/" + VIDEO)
    assert text == ""
    assert why == "transcript_empty"


# ── the live path ────────────────────────────────────────────────────────────

@pytest.mark.network
def test_a_real_video_returns_its_transcript():
    text, _title, why = media.transcript(
        "https://www.youtube.com/watch?v=" + VIDEO, limit=500)
    if why == "transcript_blocked_from_this_address":
        pytest.skip("YouTube blocks this address; expected on cloud hosts")
    assert why == ""
    assert len(text) > 100
