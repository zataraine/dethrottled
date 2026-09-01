"""Video pages, read as their transcript.

A YouTube page is not an article and never will be: fetching it gives you a
JavaScript shell, and rendering it gives you a player. The words are in the
caption track, which is served separately and is free to read.

So a video URL is handled before the fetch ladder ever runs, because the ladder
cannot win here -- no tier, however clever, extracts prose from a video player.
Recognising the URL and asking for the captions is both faster and the only
thing that actually works.

## The honest caveat

`youtube-transcript-api` talks to YouTube's own internal endpoint, with no key
and no account. YouTube blocks datacentre address ranges aggressively, so this
works from a home or office connection and frequently does not from a cloud
server. That is a real limitation and it is reported rather than hidden: a
blocked request comes back with `transcript_blocked` and the ladder is told
plainly, instead of the page being recorded as an ordinary failure.

For a tool meant to run on a machine you own, that trade is the right way
round. It is also why this is optional.

## What it deliberately does not do

It does not transcribe audio. Speech-to-text means a model, a few hundred
megabytes and minutes of CPU per video, which is a different product. A video
with no captions is reported as having none.
"""
from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

# Enabled by default: it costs nothing when the URL is not a video, and the
# dependency is optional anyway.
ENABLED = os.environ.get("DETHROTTLED_ENABLE_TRANSCRIPTS", "1").strip() not in (
    "0", "false", "no", "off", "")

# Preference order, not a filter. A caption track in any language beats no
# transcript at all, so this decides which to take when several exist rather
# than which to refuse.
LANGUAGES = [x.strip() for x in os.environ.get(
    "DETHROTTLED_TRANSCRIPT_LANGS", "en,en-GB,en-US").split(",") if x.strip()]

# An eleven-character id of exactly this alphabet. Matching loosely here would
# turn every youtube.com link -- a channel, a search, the home page -- into a
# transcript lookup that could only fail.
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                  "music.youtube.com", "youtu.be", "www.youtu.be"}


def video_id(url: str) -> str:
    """The video id in a URL, or "" if there is not one.

    Four shapes, all common in the wild: the watch page, the short link, the
    embed used on other sites, and the shorts player.
    """
    try:
        parts = urlparse(url or "")
    except ValueError:
        return ""
    host = (parts.netloc or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return ""

    if host.endswith("youtu.be"):
        candidate = parts.path.lstrip("/").split("/")[0]
        return candidate if _VIDEO_ID.match(candidate) else ""

    if parts.path == "/watch":
        candidate = (parse_qs(parts.query).get("v") or [""])[0]
        return candidate if _VIDEO_ID.match(candidate) else ""

    for prefix in ("/embed/", "/shorts/", "/live/", "/v/"):
        if parts.path.startswith(prefix):
            candidate = parts.path[len(prefix):].split("/")[0]
            return candidate if _VIDEO_ID.match(candidate) else ""
    return ""


def is_video(url: str) -> bool:
    return bool(video_id(url))


def transcript(url: str, limit: int = 20000) -> tuple:
    """(text, title, reason) for a video URL.

    Never raises. Every failure has a name, because "no transcript" and "this
    address is blocked" call for completely different responses from whoever
    is reading the logs.
    """
    if not ENABLED:
        return "", "", "transcripts_disabled"
    ident = video_id(url)
    if not ident:
        return "", "", "not_a_video_url"

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return "", "", "transcript_api_not_installed"

    try:
        fetched = YouTubeTranscriptApi().fetch(ident, languages=LANGUAGES)
    except Exception as exc:
        name = type(exc).__name__
        # These three are the ones worth telling apart. The rest are reported
        # under their own class name, which is more use than "failed".
        if "IpBlocked" in name or "RequestBlocked" in name:
            return "", "", "transcript_blocked_from_this_address"
        if "NoTranscript" in name or "TranscriptsDisabled" in name:
            return "", "", "no_transcript_available"
        if "VideoUnavailable" in name:
            return "", "", "video_unavailable"
        return "", "", "transcript_%s" % name

    # Captions arrive as timed fragments, often mid-sentence. Joined into
    # continuous prose, because every consumer of this -- an extractor, an
    # embedder, a reader -- wants sentences rather than a subtitle file.
    pieces = []
    size = 0
    for snippet in fetched:
        text = (getattr(snippet, "text", None)
                or (snippet.get("text") if isinstance(snippet, dict) else "")
                or "").strip()
        if not text or (text.startswith("[") and text.endswith("]")):
            # Bracketed cues are sound descriptions ("[Music]"), not speech.
            continue
        pieces.append(text)
        size += len(text) + 1
        if size >= limit:
            break

    joined = " ".join(pieces)
    joined = re.sub(r"\s+", " ", joined).strip()
    if not joined:
        return "", "", "transcript_empty"
    return joined[:limit], "", ""
