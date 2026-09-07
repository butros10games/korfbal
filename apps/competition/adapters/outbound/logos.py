"""Read observed public club badges without forwarding login credentials."""

import base64
from collections.abc import Callable

import requests

from apps.club.models import Club as AppClub
from apps.competition.application.ports import FetchResult, RequestGate, TransportError
from apps.competition.models import Club
from apps.competition.services.logos import MAX_IMAGE_BYTES, logo_name


def fetch_logo(
    source_id: str, gate: RequestGate | None, retry_delay: Callable[[str], int]
) -> FetchResult:
    """Fetch one bounded image, or reuse the immutable storage cache.

    Raises:
        TransportError: The image transfer failed or exceeded the size limit.

    """
    club = Club.objects.get(external_id=source_id)
    name = logo_name(club.logo_bucket, club.logo_hash)
    if AppClub().logo.storage.exists(name):
        return FetchResult(200, {"name": name})
    if gate:
        gate.before_request()
    try:
        with requests.get(
            f"https://binaries.sportlink.com/{club.logo_bucket}/{club.logo_hash}",
            timeout=30,
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.status_code != requests.codes.ok:
                return FetchResult(
                    response.status_code,
                    retry_after=retry_delay(response.headers.get("Retry-After", "60")),
                )
            raw = bytearray()
            for chunk in response.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > MAX_IMAGE_BYTES:
                    raise TransportError("Club logo exceeds size limit")
    except requests.RequestException as exc:
        raise TransportError("Club logo download failed") from exc
    return FetchResult(
        200, {"name": name, "image": base64.b64encode(raw).decode("ascii")}
    )
