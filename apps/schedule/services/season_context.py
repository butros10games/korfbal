"""Record a season's competition context once, never silently changing it."""

from __future__ import annotations

from datetime import date

from apps.schedule.domain.competition_context import (
    PHASE_DISCIPLINE,
    SOURCE_IMPORTER,
    edition_for_day,
    validate_context,
)
from apps.schedule.models import Season


def ensure_context(
    season: Season, *, edition: int, phase: str, source: str = SOURCE_IMPORTER
) -> Season:
    """Fill unresolved context fields and verify established ones.

    Returns:
        The season with its context recorded.

    Raises:
        ValueError: The season already belongs to another context or its dates
            contradict the requested edition.

    """
    desired: dict[str, object] = {
        "edition": edition,
        "discipline": PHASE_DISCIPLINE[phase],
        "phase": phase,
    }
    conflicts = sorted(
        field
        for field, value in desired.items()
        if getattr(season, field) not in {None, ""} and getattr(season, field) != value
    )
    if conflicts:
        raise ValueError(
            f"{season.name} already has another competition context: {conflicts}"
        )
    issues = validate_context(
        start=season.start_date,
        end=season.end_date,
        edition=edition,
        discipline=str(desired["discipline"]),
        phase=phase,
    )
    if issues:
        raise ValueError(f"{season.name} cannot belong to {edition}: {issues}")
    updates = {
        field: value
        for field, value in desired.items()
        if getattr(season, field) in {None, ""}
    }
    if updates:
        if not season.context_source:
            updates["context_source"] = source
        Season.objects.filter(pk=season.pk).update(**updates)
        for field, value in updates.items():
            setattr(season, field, value)
    return season


def edition_season(
    edition: int,
    phase: str,
    name: str,
    defaults: tuple[date, date] | None,
    *,
    source: str = SOURCE_IMPORTER,
) -> Season:
    """Find a playing season by its stored context, then by its importer name.

    The name is only a fallback for seasons that predate stored context; the
    context it receives is the importer's explicit decision, not a parse of
    the name.

    Returns:
        The season, created from ``defaults`` when allowed.

    Raises:
        ValueError: The context or name is ambiguous, or the season is missing.

    """
    stored = list(Season.objects.filter(edition=edition, phase=phase))
    if len(stored) > 1:
        raise ValueError(f"More than one {phase} season belongs to {edition}")
    if stored:
        season = stored[0]
    else:
        matches = list(Season.objects.filter(name__iexact=name))
        if len(matches) > 1:
            raise ValueError(f"More than one season is named {name!r}")
        if matches:
            season = matches[0]
        elif defaults is None:
            raise ValueError(f"Season {name!r} does not exist; seed the edition first")
        else:
            season = Season.objects.create(
                name=name, start_date=defaults[0], end_date=defaults[1]
            )
    return ensure_context(season, edition=edition, phase=phase, source=source)


def record_edition(
    season: Season, edition: int, *, source: str = SOURCE_IMPORTER
) -> None:
    """Record an explicitly configured edition for a provider import scope.

    Provider scopes can run a few weeks past June, so only the start date has to
    fall inside the edition.

    Raises:
        ValueError: The season already belongs to another edition or starts
            outside it.

    """
    if season.edition is not None:
        if season.edition != edition:
            raise ValueError(f"{season.name} already belongs to {season.edition}")
        return
    if edition_for_day(season.start_date) != edition:
        raise ValueError(f"{season.name} does not start in edition {edition}")
    updates: dict[str, object] = {"edition": edition}
    if not season.context_source:
        updates["context_source"] = source
    Season.objects.filter(pk=season.pk).update(**updates)
    for field, value in updates.items():
        setattr(season, field, value)
