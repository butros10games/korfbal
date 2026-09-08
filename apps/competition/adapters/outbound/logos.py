"""Read observed club badges through the authorized provider image transport."""

import base64
from collections.abc import Callable

from django.core.files.storage import Storage
import requests

from apps.club.models import Club as AppClub
from apps.competition.application.ports import FetchResult, RequestGate, TransportError
from apps.competition.models import Club
from apps.competition.services.logos import MAX_IMAGE_BYTES, logo_name


def fetch_logo(
    source_id: str,
    gate: RequestGate | None,
    retry_delay: Callable[[str], int],
    *,
    request: Callable[[str], requests.Response] | None = None,
) -> FetchResult:
    """Fetch one bounded image, or reuse the immutable storage cache."""
    club = Club.objects.get(external_id=source_id)
    name = logo_name(club.logo_bucket, club.logo_hash)
    return fetch_image(
        (club.logo_bucket, club.logo_hash, name),
        AppClub().logo.storage,
        gate,
        retry_delay,
        request=request,
    )


def fetch_image(
    reference: tuple[str, str, str],
    storage: Storage,
    gate: RequestGate | None,
    retry_delay: Callable[[str], int],
    *,
    request: Callable[[str], requests.Response] | None = None,
) -> FetchResult:
    """Read a bounded binary through the shared authorized transport and cache.

    Raises:
        TransportError: The image transfer failed or exceeded the size limit.

    """
    bucket, digest, name = reference
    if storage.exists(name):
        return FetchResult(200, {"name": name})
    if gate and request is None:
        gate.before_request()
    try:
        url = f"https://binaries.sportlink.com/{bucket}/{digest}"
        response = (
            request(url)
            if request
            else requests.get(url, timeout=30, stream=True, allow_redirects=False)
        )
        with response as image_response:
            if image_response.status_code != requests.codes.ok:
                return FetchResult(
                    image_response.status_code,
                    retry_after=retry_delay(
                        image_response.headers.get("Retry-After", "60")
                    ),
                )
            raw = bytearray()
            for chunk in image_response.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > MAX_IMAGE_BYTES:
                    raise TransportError("Club logo exceeds size limit")
    except requests.RequestException as exc:
        raise TransportError("Club logo download failed") from exc
    return FetchResult(
        200, {"name": name, "image": base64.b64encode(raw).decode("ascii")}
    )
