"""Spotify URL helpers used by the song cache."""

from __future__ import annotations

from urllib.parse import urlparse


MIN_TRACK_PATH_PARTS = 2


def canonicalize_spotify_track_url(value: str) -> str:
    """Return a canonical spotify track URL for caching.

    Normalizes supported open.spotify.com track URLs by:
    - enforcing https
    - dropping query/fragment
    - removing optional locale prefix (/intl-xx/)

    Raises:
        ValueError: if value is not a supported track URL.

    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Spotify URL is required")

    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise ValueError("Spotify URL must start with https://")

    netloc = (parsed.netloc or "").lower()
    if netloc not in {"open.spotify.com", "www.open.spotify.com"}:
        raise ValueError("Spotify URL must be an open.spotify.com track URL")

    parts = [p for p in (parsed.path or "").split("/") if p]
    if len(parts) >= MIN_TRACK_PATH_PARTS and parts[0].startswith("intl-"):
        parts = parts[1:]

    if len(parts) < MIN_TRACK_PATH_PARTS or parts[0] != "track" or not parts[1].strip():
        raise ValueError("Spotify URL must point to a track")

    track_id = parts[1].strip()
    return f"https://open.spotify.com/track/{track_id}"
