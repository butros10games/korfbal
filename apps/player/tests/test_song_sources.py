"""Pure URL contracts for supported song imports."""

import pytest

from apps.player.song_sources import parse_song_source


VIDEO_ID = "BaW_jenozKc"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


@pytest.mark.parametrize(
    "url",
    [
        CANONICAL_URL,
        f"https://youtube.com/watch?v={VIDEO_ID}&list=PLignored&index=3",
        f"https://youtu.be/{VIDEO_ID}?si=share-tracking",
        f"https://www.youtu.be/{VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://music.youtube.com/watch?v={VIDEO_ID}&list=RDignored",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
        f"  http://WWW.YOUTUBE.COM/watch?v={VIDEO_ID}  ",
    ],
)
def test_shared_youtube_links_have_one_cache_identity(url: str) -> None:
    """Sharing parameters and supported host/path variants identify one source."""
    source = parse_song_source(url)
    assert source.url == CANONICAL_URL
    assert source.provider == "youtube"
    assert source.start_seconds == 0


@pytest.mark.parametrize(
    ("suffix", "seconds"),
    [
        ("&t=42", 42),
        ("&t=1m12s", 72),
        ("&start=23", 23),
        ("#t=30s", 30),
        ("&t=0", 0),
        ("&t=14m59s", 899),
    ],
)
def test_timestamp_is_player_state_not_cache_identity(
    suffix: str, seconds: int
) -> None:
    """Keep the full source shared while preserving each player's chosen offset."""
    source = parse_song_source(CANONICAL_URL + suffix)
    assert source.url == CANONICAL_URL
    assert source.start_seconds == seconds


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///etc/passwd",
        "http://127.0.0.1/audio",
        "https://example.com/song",
        f"https://youtube.com.evil.example/watch?v={VIDEO_ID}",
        f"https://youtube.com@evil.example/watch?v={VIDEO_ID}",
        f"https://user:password@www.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com:8443/watch?v={VIDEO_ID}",
        "https://www.youtube.com/playlist?list=PLexample",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/watch?v=short",
        f"https://youtu.be/{VIDEO_ID}/extra",
        CANONICAL_URL + "&v=xxxxxxxxxxx",
        CANONICAL_URL + "&t=-1",
        CANONICAL_URL + "&t=nonsense",
        CANONICAL_URL + "&t=900",
        CANONICAL_URL + "&t=1h",
    ],
)
def test_unsupported_sources_are_rejected(url: str) -> None:
    """Reject unsupported inputs before any external operation."""
    with pytest.raises(ValueError, match=r"Provide|Invalid|Start time"):
        parse_song_source(url)


def test_spotify_track_import_stays_compatible() -> None:
    """Existing locale and sharing links retain the original track identity."""
    source = parse_song_source(
        "https://open.spotify.com/intl-nl/track/example?si=share"
    )
    assert source.url == "https://open.spotify.com/track/example"
    assert source.provider == "spotify"
