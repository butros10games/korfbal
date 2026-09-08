"""Publish permitted roster photos into the native player image field."""

import hashlib
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.competition.models import SyncResource
from apps.competition.services.logos import logo_name, store_image
from apps.player.models import Player
from apps.schedule.models import Season


PREFIX = "profile_pictures/knkv/"


def photo_name(player: Player) -> str:
    """Keep cached files private to an identity so withdrawal can erase them."""
    digest = hashlib.sha256(player.knkv_photo.encode()).hexdigest()[:32]
    return f"{PREFIX}{player.pk}/{digest}.png"


def discover_photo(player: Player, reference: object, season: Season) -> None:
    """Queue changed permitted images and remove withdrawn imported files."""
    desired = _permitted_reference(player, reference)
    current = player.profile_picture.name or ""
    # A native upload always wins, including when an account claims an import.
    if current and not current.startswith(PREFIX):
        desired = ""
    previous = player.knkv_photo
    if previous != desired:
        old_name = photo_name(player) if previous else ""
        player.knkv_photo = desired
        fields = ["knkv_photo"]
        if current.startswith(PREFIX):
            player.profile_picture = ""
            fields.append("profile_picture")
        player.save(update_fields=fields)
        if old_name:
            storage = player.profile_picture.storage
            transaction.on_commit(lambda: storage.delete(old_name))
    if not desired or (current == photo_name(player) and previous == desired):
        return
    resource, created = SyncResource.objects.get_or_create(
        season=season,
        kind="player_photo",
        source_id=str(player.pk),
        defaults={"next_sync_at": timezone.now()},
    )
    if not created and (
        previous != desired
        or (resource.fetched_at is not None and not resource.failures)
    ):
        resource.fetched_at = None
        resource.next_sync_at = timezone.now()
        resource.failures = 0
        resource.etag = ""
        resource.save(update_fields=("fetched_at", "next_sync_at", "failures", "etag"))


@transaction.atomic
def cache_photo(source_id: str, data: dict[str, Any]) -> None:
    """Reject stale responses and preserve native uploads made during download."""
    player = Player.objects.select_for_update().filter(pk=source_id).first()
    if (
        player is None
        or not player.knkv_photo
        or player.knkv_privacy not in {"OPEN", "NORMAL"}
    ):
        return
    current = player.profile_picture.name or ""
    if current and not current.startswith(PREFIX):
        return
    name = photo_name(player)
    if data.get("name") != name:
        return
    player.profile_picture = store_image(player.profile_picture.storage, name, data)
    player.save(update_fields=("profile_picture",))


def _permitted_reference(player: Player, reference: object) -> str:
    """Reject unknown buckets, malformed references and name-only privacy."""
    desired = ""
    if isinstance(reference, dict) and player.knkv_privacy in {"OPEN", "NORMAL"}:
        bucket, digest = reference.get("Bucket"), reference.get("Hash")
        if isinstance(bucket, str) and isinstance(digest, str):
            try:
                logo_name(bucket, digest)
            except ValueError:
                pass
            else:
                desired = f"{bucket}/{digest.upper()}"
    return desired
