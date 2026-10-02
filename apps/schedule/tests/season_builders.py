"""Build playing seasons with an explicit competition context for tests."""

from __future__ import annotations

from datetime import date

from apps.schedule.domain.competition_context import (
    AUTUMN,
    FULL_SEASON,
    INDOOR_PHASE,
    PHASE_DISCIPLINE,
    SPRING,
)
from apps.schedule.models import Season


DEFAULT_NAMES = {
    AUTUMN: "Voor seizoen {edition}",
    SPRING: "Na seizoen {next}",
    INDOOR_PHASE: "Zaal seizoen {edition}-{next}",
    FULL_SEASON: "Veld seizoen {edition}-{next}",
}


def phase_dates(phase: str, edition: int) -> tuple[date, date]:
    """Return the importer's dates for a playing season of an edition."""
    return {
        AUTUMN: (date(edition, 7, 1), date(edition, 12, 31)),
        SPRING: (date(edition + 1, 1, 1), date(edition + 1, 6, 30)),
        INDOOR_PHASE: (date(edition, 10, 1), date(edition + 1, 6, 30)),
        FULL_SEASON: (date(edition, 7, 1), date(edition + 1, 6, 30)),
    }[phase]


def playing_season(
    phase: str,
    edition: int,
    *,
    name: str | None = None,
    dates: tuple[date, date] | None = None,
) -> Season:
    """Create a season whose context is stored, as the importer records it."""
    start, end = dates or phase_dates(phase, edition)
    return Season.objects.create(
        name=name or DEFAULT_NAMES[phase].format(edition=edition, next=edition + 1),
        start_date=start,
        end_date=end,
        edition=edition,
        discipline=PHASE_DISCIPLINE[phase],
        phase=phase,
        context_source="importer",
    )
