"""Canonical identities and per-player start times for imported songs."""

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlparse

from apps.player.spotify import canonicalize_spotify_track_url


MAX_SONG_SECONDS = 900
VIDEO_PATH_PARTS = 2
YOUTUBE_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
})


@dataclass(frozen=True, slots=True)
class SongSource:
    """One share-independent cache identity with optional playback offset."""

    url: str
    provider: str
    start_seconds: int = 0


def _start_seconds(value: str) -> int:
    if value.isascii() and value.isdecimal():
        seconds = int(value)
    else:
        match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value, re.ASCII)
        if not match or not value:
            raise ValueError("Invalid YouTube start time.")
        hours, minutes, seconds_part = (int(part or 0) for part in match.groups())
        seconds = hours * 3600 + minutes * 60 + seconds_part
    if seconds >= MAX_SONG_SECONDS:
        raise ValueError("Start time must be less than 15 minutes.")
    return seconds


def parse_song_source(value: str) -> SongSource:
    """Accept a Spotify track or one YouTube video, never an arbitrary fetch URL.

    Raises:
        ValueError: The link, video identity or start time is unsupported.

    """
    parsed = urlparse(value.strip())
    host = parsed.netloc.lower()
    if host in {"open.spotify.com", "www.open.spotify.com"}:
        return SongSource(canonicalize_spotify_track_url(value), "spotify")
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        raise ValueError("Provide a Spotify track or YouTube video link.")

    query = parse_qs(parsed.query)
    parts = parsed.path.strip("/").split("/")
    video_id = ""
    if host in {"youtu.be", "www.youtu.be"} and len(parts) == 1:
        video_id = parts[0]
    elif parts == ["watch"] and len(query.get("v", [])) == 1:
        video_id = query["v"][0]
    elif len(parts) == VIDEO_PATH_PARTS and parts[0] in {"shorts", "embed", "live"}:
        video_id = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError(
            "Provide a link to one YouTube video, not a playlist or channel."
        )

    fragment = parse_qs(parsed.fragment)
    start = query.get("t") or query.get("start") or fragment.get("t")
    return SongSource(
        f"https://www.youtube.com/watch?v={video_id}",
        "youtube",
        _start_seconds(start[0]) if start else 0,
    )
