"""Cache provider club badges and publish them without replacing user uploads."""

import base64
from io import BytesIO
import re
from typing import Any

from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image

from apps.club.models import Club as AppClub
from apps.competition.models import Club, SyncResource
from apps.schedule.models import Season


MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_SIDE = 4096


def logo_name(bucket: str, digest: str) -> str:
    """Validate the observed binary reference and return its immutable cache key.

    Raises:
        ValueError: The reference is not a KNKV image identifier.

    """
    if bucket not in {
        "KNKV-production-REPL",
        "KNKV-production-EXTERNAL",
    } or not re.fullmatch(r"[A-Fa-f0-9]{1,32}", digest):
        raise ValueError("Invalid club logo reference")
    return f"club_pictures/knkv/{bucket}/{digest.upper()}.png"


def discover_logo(club: Club, reference: object, season: Season) -> None:
    """Retain valid logo references and enqueue only missing or changed images."""
    if not isinstance(reference, dict):
        return
    bucket, digest = str(reference.get("Bucket", "")), str(reference.get("Hash", ""))
    try:
        name = logo_name(bucket, digest)
    except ValueError:
        return
    changed = (club.logo_bucket, club.logo_hash) != (bucket, digest)
    if changed:
        club.logo_bucket, club.logo_hash = bucket, digest
        club.save(update_fields=("logo_bucket", "logo_hash"))
    if club.cached_logo == name:
        return
    resource, created = SyncResource.objects.get_or_create(
        season=season,
        kind="club_logo",
        source_id=club.external_id,
        defaults={"next_sync_at": timezone.now()},
    )
    if changed and not created:
        resource.fetched_at = None
        resource.next_sync_at = timezone.now()
        resource.etag = ""
        resource.save(update_fields=("fetched_at", "next_sync_at", "etag"))


def cache_logo(source_id: str, data: dict[str, Any]) -> None:
    """Validate image bytes before storing a small, metadata-free PNG.

    Raises:
        ValueError: The payload is stale, oversized or not a supported image.

    """
    club = Club.objects.get(external_id=source_id)
    name = logo_name(club.logo_bucket, club.logo_hash)
    if data.get("name") != name:
        raise ValueError("Stale club logo response")
    storage = AppClub().logo.storage
    if not storage.exists(name):
        raw = base64.b64decode(data["image"], validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("Club logo exceeds size limit")
        try:
            with Image.open(BytesIO(raw)) as image:
                if max(image.size) > MAX_IMAGE_SIDE:
                    raise ValueError("Club logo exceeds dimension limit")
                image.thumbnail((512, 512))
                output = BytesIO()
                image.convert("RGBA").save(output, format="PNG")
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError("Invalid club logo image") from exc
        name = storage.save(name, ContentFile(output.getvalue()))
    club.cached_logo = name
    club.save(update_fields=("cached_logo",))
    if club.local_club_id:
        publish_logo(club)


def publish_logo(club: Club) -> None:
    """Use the native image field, keeping manually uploaded images authoritative."""
    local = club.local_club
    if local is None or not club.cached_logo:
        return
    current = local.logo.name or ""
    if current and current != club.published_logo:
        return
    if current != club.cached_logo and not AppClub.objects.filter(
        pk=local.pk, logo=local.logo.name
    ).update(logo=club.cached_logo):
        return
    if club.published_logo != club.cached_logo:
        club.published_logo = club.cached_logo
        club.save(update_fields=("published_logo",))
