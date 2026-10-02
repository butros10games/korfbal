"""Shared season selection and API option helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from django.db.models import Max, Min, Q
from django.utils import timezone

from apps.schedule.models import Match, Season


# A korfbal year runs from July to June and holds up to four playing seasons.
EDITION_FIRST_MONTH = 7
AUTUMN = "autumn"
INDOOR = "indoor"
SPRING = "spring"
FULL_YEAR = "full_year"
OTHER = "other"


def season_edition(season: Season) -> int | None:
    """Return the edition a season belongs to, or None when it is unresolved.

    The stored edition wins; otherwise a season that fits inside one July-June
    year belongs to it. Spring seasons (January-June) belong to the edition that
    started the previous July.
    """
    return season.context.edition


def season_kind(season: Season) -> str:
    """Classify a season by its stored phase; renaming never changes the kind."""
    return season.context.kind


@dataclass(frozen=True)
class FoldedSeason:
    """The part of a full-year outdoor season shown inside one of its halves."""

    whole: Season
    start: datetime | None
    end: datetime | None


def _outdoor_parts(editions: set[int]) -> dict[int, dict[str, Season]]:
    """Return the outdoor seasons of editions whose halves are unambiguous."""
    if not editions:
        return {}
    found: dict[int, dict[str, list[Season]]] = {}
    for season in Season.objects.filter(
        start_date__gte=date(min(editions), EDITION_FIRST_MONTH, 1),
        start_date__lt=date(max(editions) + 1, EDITION_FIRST_MONTH, 1),
    ):
        edition = season_edition(season)
        if edition is not None and edition in editions:
            found.setdefault(edition, {}).setdefault(season_kind(season), []).append(
                season
            )
    return {
        edition: {kind: rows[0] for kind, rows in kinds.items()}
        for edition, kinds in found.items()
        if all(len(kinds.get(kind, [])) == 1 for kind in (AUTUMN, SPRING, FULL_YEAR))
    }


def fold_full_year_seasons(seasons: list[Season]) -> list[Season]:
    """Replace full-year outdoor seasons by the two outdoor halves of their edition.

    Poules that play both halves live in their own season. Lists covering many
    teams offer the halves instead, so every team appears under the same choices.
    A full-year season without both halves stays a choice of its own.
    """
    parts = _outdoor_parts({
        edition
        for season in seasons
        if season_kind(season) == FULL_YEAR
        and (edition := season_edition(season)) is not None
    })
    if not parts:
        return seasons
    hidden = {kinds[FULL_YEAR].pk for kinds in parts.values()}
    folded = {season.pk: season for season in seasons if season.pk not in hidden}
    for kinds in parts.values():
        folded.update({kinds[kind].pk: kinds[kind] for kind in (AUTUMN, SPRING)})
    return sorted(
        folded.values(),
        key=lambda season: (-season.start_date.toordinal(), season.name),
    )


def folded_full_year(season: Season | None) -> FoldedSeason | None:
    """Return the full-year season whose matches a half season also shows."""
    kind = season_kind(season) if season else OTHER
    if season is None or kind not in {AUTUMN, SPRING}:
        return None
    edition = season_edition(season)
    if edition is None:
        return None
    parts = _outdoor_parts({edition}).get(edition)
    if parts is None or parts[kind].pk != season.pk:
        return None
    # Full-year matches follow the calendar year, like the importer's routing.
    turn = datetime(edition + 1, 1, 1, tzinfo=timezone.get_current_timezone())
    return FoldedSeason(
        parts[FULL_YEAR],
        start=None if kind == AUTUMN else turn,
        end=turn if kind == AUTUMN else None,
    )


def full_year_for_half(requested_id: str, seasons: list[Season]) -> Season | None:
    """Return the scoped full-year season covering a requested outdoor half.

    Club pages offer the halves for every team, so a team that plays one
    full-year outdoor season is opened with a half it does not have itself.
    """
    whole = [season for season in seasons if season_kind(season) == FULL_YEAR]
    if not whole:
        return None
    try:
        half = Season.objects.filter(pk=UUID(requested_id)).first()
    except ValueError:
        return None
    if half is None or season_kind(half) not in {AUTUMN, SPRING}:
        return None
    edition = season_edition(half)
    return next(
        (
            season
            for season in whole
            if edition is not None and season_edition(season) == edition
        ),
        None,
    )


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
            "edition": season.context.edition,
            "kind": season.context.kind,
            "discipline": season.context.discipline,
            "phase": season.context.phase,
        }
        for season in seasons
    ]
