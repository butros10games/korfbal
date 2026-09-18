"""Download ordinary public Eyecons MP4 playback sources for local review."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import urlsplit

from .media import binary


MAX_DOWNLOAD_BYTES = 6_000_000_000
HTTP_TIMEOUT = 1800


def fetch_text(url: str) -> str:
    """Fetch public metadata using curl's standard HTTPS/proxy handling."""
    result = subprocess.run(
        [
            binary("curl"),
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-time",
            "30",
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=40,
    )
    return result.stdout


def discover(page_url: str) -> dict[str, Any]:
    """Resolve a published public recording from Eyecons's embedded metadata.

    Raises:
        ValueError: If the page or public MP4 source is not supported.

    """
    page = urlsplit(page_url)
    if (
        page.scheme != "https"
        or page.netloc not in {"eyecons.com", "www.eyecons.com"}
        or not page.path.startswith("/videos/")
    ):
        raise ValueError("Use an https://eyecons.com/videos/... match page")
    html = fetch_text(page_url)
    embedded = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not embedded:
        raise ValueError("Eyecons page no longer exposes supported metadata")
    video = (
        json
        .loads(embedded.group(1))
        .get("props", {})
        .get("pageProps", {})
        .get("video", {})
    )
    external_id = str(video.get("external_id", ""))
    if video.get("is_premium") or not re.fullmatch(r"[A-Za-z0-9]{8}", external_id):
        raise ValueError(
            "No supported public playback source; use a local authorized recording"
        )
    feed = json.loads(fetch_text(f"https://cdn.jwplayer.com/v2/media/{external_id}"))
    playlist = feed.get("playlist", [])
    sources = playlist[0].get("sources", []) if playlist else []
    mp4 = [
        s
        for s in sources
        if s.get("type") == "video/mp4"
        and urlsplit(s.get("file", "")).netloc == "cdn.jwplayer.com"
        and urlsplit(s.get("file", "")).scheme == "https"
    ]
    if not mp4:
        raise ValueError(
            "No public MP4 source found; protected streams are not downloaded"
        )
    chosen = max(mp4, key=lambda source: int(source.get("height", 0)))
    return {
        "title": video.get("title", "Eyecons match"),
        "source_url": page_url,
        "media_url": chosen["file"],
        "height": chosen.get("height"),
        "external_id": external_id,
    }


def download(source: dict[str, Any], destination: Path) -> None:
    """Download at most 6 GB, leaving no partial final recording on failure."""
    temporary = destination.with_suffix(".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                binary("curl"),
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--retry",
                "2",
                "--max-time",
                str(HTTP_TIMEOUT),
                "--max-filesize",
                str(MAX_DOWNLOAD_BYTES),
                "--output",
                str(temporary),
                source["media_url"],
            ],
            check=True,
            capture_output=True,
            timeout=HTTP_TIMEOUT + 60,
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
