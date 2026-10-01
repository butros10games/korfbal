"""Shared season selection and API option helpers."""

from __future__ import annotations

from django.db.models import Max, Min, Q
from django.utils import timezone

from apps.schedule.models import Match, Season


def _playing_season(active: list[Season]) -> Season:
    """Choose among overlapping running seasons by where matches are played.

    Indoor seasons start inside the outdoor edition while outdoor fixtures are
    still being played, so date ranges alone cannot decide which one is current.
    Prefer the season holding the next fixture, then the most recently played one,
    and only then the latest-starting season.
    """
    by_start = sorted(
        active,
        key=lambda season: (
            -season.start_date.toordinal(),
            season.end_date,
            str(season.pk),
        ),
    )
    if len(by_start) == 1:
        return by_start[0]
    now = timezone.now()
    activity = {
        row["season_id"]: row
        for row in Match.objects
        .filter(season__in=by_start)
        .values("season_id")
        .annotate(
            next_start=Min("start_time", filter=Q(start_time__gte=now)),
            last_start=Max("start_time", filter=Q(start_time__lt=now)),
        )
    }
    upcoming = [
        season
        for season in by_start
        if activity.get(season.pk, {}).get("next_start") is not None
    ]
    if upcoming:
        return min(upcoming, key=lambda season: activity[season.pk]["next_start"])
    played = [
        season
        for season in by_start
        if activity.get(season.pk, {}).get("last_start") is not None
    ]
    if played:
        return max(played, key=lambda season: activity[season.pk]["last_start"])
    return by_start[0]


def current_season() -> Season | None:
    """Return the running season where play currently takes place."""
    today = timezone.localdate()
    active = list(Season.objects.filter(start_date__lte=today, end_date__gte=today))
    return _playing_season(active) if active else None


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
    today = timezone.localdate()
    active = [
        season for season in seasons if season.start_date <= today <= season.end_date
    ]
    if active:
        return _playing_season(active)
    previous = [season for season in seasons if season.start_date <= today]
    return (
        max(previous, key=lambda season: (season.start_date, str(season.pk)))
        if previous
        else min(seasons, key=lambda season: (season.start_date, str(season.pk)))
    )


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
    today = timezone.localdate()
    active = default_season([
        season for season in seasons if season.start_date <= today <= season.end_date
    ])
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
