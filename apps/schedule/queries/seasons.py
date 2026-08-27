"""Shared season selection and API option helpers."""

from __future__ import annotations

from django.utils import timezone

from apps.schedule.models import Season


def current_season() -> Season | None:
    """Return the season containing today's local date."""
    today = timezone.localdate()
    return Season.objects.filter(start_date__lte=today, end_date__gte=today).first()


def most_recent_season() -> Season | None:
    """Return the most recently completed season."""
    return (
        Season.objects
        .filter(end_date__lte=timezone.localdate())
        .order_by("-end_date")
        .first()
    )


def find_season(requested_id: str, seasons: list[Season]) -> Season | None:
    """Find a requested season within an already scoped collection."""
    return next(
        (season for season in seasons if str(season.id_uuid) == requested_id),
        None,
    )


def default_season(seasons: list[Season]) -> Season | None:
    """Prefer the current scoped season, then the most recent option."""
    if not seasons:
        return None
    active = current_season()
    if active and any(season.id_uuid == active.id_uuid for season in seasons):
        return active
    return seasons[0]


def requested_or_default_season(
    requested_id: str | None,
    seasons: list[Season],
) -> Season | None:
    """Resolve a scoped request, safely falling back for invalid identifiers."""
    requested = find_season(requested_id, seasons) if requested_id else None
    return requested or default_season(seasons)


def season_options_payload(seasons: list[Season]) -> list[dict[str, object]]:
    """Serialize season choices consistently across overview endpoints."""
    if not seasons:
        return []
    active = current_season()
    return [
        {
            "id_uuid": str(season.id_uuid),
            "name": season.name,
            "start_date": season.start_date.isoformat(),
            "end_date": season.end_date.isoformat(),
            "is_current": active is not None and season.id_uuid == active.id_uuid,
        }
        for season in seasons
    ]
