"""Retrieve permitted photos with the existing authenticated binary transport."""

from collections.abc import Callable

import requests

from apps.competition.application.ports import FetchResult, RequestGate
from apps.competition.services.logos import logo_name
from apps.competition.services.player_photos import photo_candidates, photo_name

from .logos import fetch_image


def fetch_photo(
    source_id: str,
    gate: RequestGate | None,
    retry_delay: Callable[[str], int],
    *,
    request: Callable[[str], requests.Response],
) -> FetchResult:
    """Skip withdrawn, stale and manually replaced photos before any request."""
    player = photo_candidates().filter(pk=source_id).first()
    if player is None:
        return FetchResult(200, {})
    current = player.profile_picture.name or ""
    name = photo_name(player)
    if current and current != name:
        return FetchResult(200, {})
    bucket, digest = player.knkv_photo.split("/")
    logo_name(bucket, digest)
    return fetch_image(
        (bucket, digest, name),
        player.profile_picture.storage,
        gate,
        retry_delay,
        request=request,
    )
