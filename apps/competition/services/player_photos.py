"""Publish permitted roster photos into the native player image field."""

from datetime import datetime, timedelta
import hashlib
from typing import Any

from django.db import transaction
from django.db.models import CharField, Q, QuerySet, Value
from django.db.models.functions import Cast, Replace
from django.utils import timezone

from apps.competition.models import SyncResource
from apps.competition.services.logos import logo_name, store_image
from apps.competition.services.resources import ENDPOINTS
from apps.player.media_paths import delete_with_variant
from apps.player.models import Player
from apps.schedule.models import Season


PREFIX = "profile_pictures/knkv/"
# Source privacy levels that permit retaining the provider's photo.
PHOTO_PRIVACY = ("OPEN", "NORMAL")


def photo_name(player: Player) -> str:
    """Keep cached files private to an identity so withdrawal can erase them."""
    digest = hashlib.sha256(player.knkv_photo.encode()).hexdigest()[:32]
    return f"{PREFIX}{player.pk}/{digest}.png"


def photo_candidates() -> QuerySet[Player]:
    """Visible identities that permit a provider photo and have no native upload.

    The same rules guard the request before I/O (``fetch_photo``) and the write
    after it (``cache_photo``): ``Player.objects`` freshness and archival,
    OPEN/NORMAL privacy and a current reference.
    """
    return (
        Player.objects
        .filter(knkv_privacy__in=PHOTO_PRIVACY)
        .exclude(knkv_photo="")
        .filter(
            Q(profile_picture__isnull=True)
            | Q(profile_picture="")
            | Q(profile_picture__startswith=PREFIX)
        )
    )


def eligible_photo_rows(rows: QuerySet[SyncResource]) -> QuerySet[SyncResource]:
    """Keep photo rows whose person currently passes ``photo_candidates``.

    Source IDs are ``str(player.pk)``. Both sides compare as 32 hex digits in an
    uncorrelated subquery: PostgreSQL casts a UUID with hyphens, SQLite stores
    it without.
    """
    people = (
        photo_candidates()
        .annotate(key=Replace(Cast("pk", output_field=CharField()), Value("-")))
        .values("key")
    )
    return rows.annotate(person_key=Replace("source_id", Value("-"))).filter(
        person_key__in=people
    )


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
            transaction.on_commit(lambda: delete_with_variant(storage, old_name))
    if not desired or (current == photo_name(player) and previous == desired):
        return
    resource, created = SyncResource.objects.get_or_create(
        season=season,
        kind="player_photo",
        source_id=str(player.pk),
        defaults={"next_sync_at": timezone.now()},
    )
    if previous != desired:
        # Retry state belongs to one person and reference: failures recorded for
        # an earlier image in another season must not block the new one.
        SyncResource.objects.filter(
            kind="player_photo",
            source_id=str(player.pk),
            fetched_at__isnull=True,
            failures__gt=0,
        ).exclude(pk=resource.pk).update(failures=0, next_sync_at=timezone.now())
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
    player = (
        photo_candidates().select_for_update(no_key=True).filter(pk=source_id).first()
    )
    if player is None:
        return
    name = photo_name(player)
    if data.get("name") != name:
        return
    player.profile_picture = store_image(player.profile_picture.storage, name, data)
    player.save(update_fields=("profile_picture",))


@transaction.atomic
def settle_photo_siblings(
    source_id: str, now: datetime, *, expected_name: str | None = None
) -> int:
    """Complete a person's other queued photo rows once the current image is saved.

    Rows are per season, but the image belongs to the person and reference.
    Only a saved picture for the current reference settles them; a response
    must also match ``expected_name`` before completing its attempted row. A skipped,
    withdrawn or changed reference leaves every other row (and its retry state)
    untouched.

    Returns:
        The number of rows settled.

    """
    player = (
        photo_candidates()
        .select_for_update(no_key=True)
        .filter(pk=source_id)
        .only("pk", "knkv_photo", "profile_picture")
        .first()
    )
    if player is None:
        return 0
    name = photo_name(player)
    if player.profile_picture.name != name or (
        expected_name is not None and expected_name != name
    ):
        return 0
    return SyncResource.objects.filter(
        kind="player_photo", source_id=source_id, fetched_at__isnull=True
    ).update(
        fetched_at=now,
        next_sync_at=now + timedelta(hours=ENDPOINTS["player_photo"][3]),
        failures=0,
        last_error="",
        etag="",
    )


def _permitted_reference(player: Player, reference: object) -> str:
    """Reject unknown buckets, malformed references and name-only privacy."""
    desired = ""
    if isinstance(reference, dict) and player.knkv_privacy in PHOTO_PRIVACY:
        bucket, digest = reference.get("Bucket"), reference.get("Hash")
        if isinstance(bucket, str) and isinstance(digest, str):
            try:
                logo_name(bucket, digest)
            except ValueError:
                pass
            else:
                desired = f"{bucket}/{digest.upper()}"
    return desired
