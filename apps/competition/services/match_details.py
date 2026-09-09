"""One-time, resumable enrichment of known Sportlink matches."""

from datetime import datetime
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from apps.competition.models import Match, SyncResource
from apps.schedule.models import Season


DETAIL_FIELDS = {
    "match_timing": "playing_time_observed_at",
    "match_facility": "facility_observed_at",
    "match_rules": "rules_observed_at",
}


def source_matches(season: Season) -> QuerySet[Match]:
    """Do not send archive or Dataservice identities to the app endpoints."""
    return (
        Match.objects
        .filter(season=season)
        .exclude(external_id__startswith="ds:")
        .exclude(external_id__startswith="archive:")
    )


def preview_details(season: Season) -> dict[str, Any]:
    """Count missing components; three missing components mean three initial GETs."""
    matches = source_matches(season)
    counts = {
        kind: matches.filter(**{field + "__isnull": True}).count()
        for kind, field in DETAIL_FIELDS.items()
    }
    return {
        "matches": matches.count(),
        "missing_by_kind": counts,
        "remaining_detail_requests": sum(counts.values()),
        "note": "One GET per missing component; OAuth and retries add requests. "
        "Existing cooldowns and failed checkpoints remain effective.",
    }


def queue_missing_details(
    season: Season,
    *,
    match_ids: set[int] | None = None,
    include_timing: bool = True,
) -> int:
    """Bulk queue missing metadata, preserving retries and completed components."""
    matches = source_matches(season)
    if match_ids is not None:
        matches = matches.filter(pk__in=match_ids)
    fields = {
        kind: field
        for kind, field in DETAIL_FIELDS.items()
        if include_timing or kind != "match_timing"
    }
    existing = set(
        SyncResource.objects.filter(
            season=season, kind__in=fields, source_id__in=matches.values("external_id")
        ).values_list("kind", "source_id")
    )
    now = timezone.now()
    pending = [
        SyncResource(
            season=season, kind=kind, source_id=row["external_id"], next_sync_at=now
        )
        for row in matches.values("external_id", *fields.values())
        for kind, field in fields.items()
        if row[field] is None and (kind, row["external_id"]) not in existing
    ]
    SyncResource.objects.bulk_create(pending, ignore_conflicts=True, batch_size=500)
    return len(pending)


def _import_metadata(
    season: Season,
    source_id: str,
    data: dict[str, Any],
    observed_at: datetime,
    *,
    component: tuple[str, str],
) -> None:
    """Keep the endpoint's structured metadata, bound to the requested source match.

    Raises:
        ValueError: The endpoint identifies a different match or has no metadata.

    """
    field, stamp = component
    if "PublicMatchId" in data and str(data["PublicMatchId"]) != source_id:
        raise ValueError("Unrecognized match metadata")
    payload = {
        key: value
        for key, value in data.items()
        if key not in {"PublicMatchId", "Error"}
    }
    if not payload:
        raise ValueError("Missing match metadata")
    match = Match.objects.get(season=season, external_id=source_id)
    previous = getattr(match, stamp)
    if previous is not None and previous > observed_at:
        return
    setattr(match, field, payload)
    setattr(match, stamp, observed_at)
    match.save(update_fields=(field, stamp))


def import_facility(
    season: Season, source_id: str, data: dict[str, Any], observed_at: datetime
) -> None:
    """Read MatchFacility, never the nullable Location in MatchResultDetails."""
    _import_metadata(
        season,
        source_id,
        data,
        observed_at,
        component=("facility_details", "facility_observed_at"),
    )


def import_rules(
    season: Season, source_id: str, data: dict[str, Any], observed_at: datetime
) -> None:
    """Retain MatchInfo rules without guessing enforcement semantics from labels."""
    _import_metadata(
        season,
        source_id,
        data,
        observed_at,
        component=("match_rules", "rules_observed_at"),
    )
