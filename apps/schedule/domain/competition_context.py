"""The authoritative competition context of a playing season.

A korfbal year (the annual *edition*) runs from July to June. Within it the
KNKV plays independent outdoor autumn and spring competitions, outdoor
competitions that continue across the winter break, and an indoor
competition. Season names are display labels: the context is stored
explicitly on the season and never derived from an editable name.

Dates are only used for the edition, and only when a season fits inside one
July-June year. Anything else stays unresolved rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


EDITION_FIRST_MONTH = 7

INDOOR = "indoor"
OUTDOOR = "outdoor"
DISCIPLINES = (INDOOR, OUTDOOR)

AUTUMN = "autumn"
SPRING = "spring"
# One outdoor competition across both halves of an edition.
FULL_SEASON = "full_season"
INDOOR_PHASE = "indoor"
PHASES = (AUTUMN, SPRING, FULL_SEASON, INDOOR_PHASE)
PHASE_DISCIPLINE = {
    AUTUMN: OUTDOOR,
    SPRING: OUTDOOR,
    FULL_SEASON: OUTDOOR,
    INDOOR_PHASE: INDOOR,
}

# Season option ``kind`` values served to clients before the context existed.
LEGACY_KIND = {
    AUTUMN: "autumn",
    SPRING: "spring",
    FULL_SEASON: "full_year",
    INDOOR_PHASE: "indoor",
}
OTHER_KIND = "other"

# Where a stored context came from.
SOURCE_IMPORTER = "importer"
SOURCE_LEGACY_NAME = "legacy_name"
SOURCE_MANUAL = "manual"
SOURCES = (SOURCE_IMPORTER, SOURCE_LEGACY_NAME, SOURCE_MANUAL)


def edition_for_day(day: date) -> int:
    """Return the edition (start year of the July-June korfbal year) of a day."""
    return day.year if day.month >= EDITION_FIRST_MONTH else day.year - 1


def edition_from_dates(start: date, end: date) -> int | None:
    """Return the edition only when both dates fall inside the same korfbal year."""
    edition = edition_for_day(start)
    return edition if edition_for_day(end) == edition else None


def edition_label(edition: int | None) -> str | None:
    """Return the conventional ``2025-2026`` label of an edition."""
    return None if edition is None else f"{edition}-{edition + 1}"


def edition_bounds(edition: int) -> tuple[date, date]:
    """Return the first and last day of an edition."""
    return date(edition, EDITION_FIRST_MONTH, 1), date(edition + 1, 6, 30)


@dataclass(frozen=True)
class SeasonContext:
    """Resolved context; ``None`` fields are explicitly unresolved."""

    edition: int | None
    discipline: str | None
    phase: str | None
    # stored | dates | unresolved, for the edition; stored context source otherwise.
    edition_source: str
    source: str

    @property
    def kind(self) -> str:
        """Return the legacy option kind clients group season choices by."""
        return LEGACY_KIND.get(self.phase or "", OTHER_KIND)

    @property
    def continuous(self) -> bool:
        """Tell whether one outdoor competition continues across the winter break."""
        return self.phase == FULL_SEASON

    def as_payload(self) -> dict[str, object]:
        """Serialize the context with its unresolved fields kept explicit."""
        return {
            "edition": self.edition,
            "edition_label": edition_label(self.edition),
            "discipline": self.discipline,
            "phase": self.phase,
            "edition_source": self.edition_source,
            "source": self.source,
        }


class StoredContext(Protocol):
    """Season fields the context is resolved from."""

    start_date: date
    end_date: date
    edition: int | None
    discipline: str
    phase: str
    context_source: str


def resolve_context(season: StoredContext) -> SeasonContext:
    """Combine stored fields with the only safe date fallback (the edition)."""
    if season.edition is not None:
        resolved, edition_source = season.edition, "stored"
    else:
        resolved = edition_from_dates(season.start_date, season.end_date)
        edition_source = "dates" if resolved is not None else "unresolved"
    return SeasonContext(
        edition=resolved,
        discipline=season.discipline or None,
        phase=season.phase or None,
        edition_source=edition_source,
        source=season.context_source or "unresolved",
    )


def validate_context(
    *, start: date, end: date, edition: int | None, discipline: str, phase: str
) -> list[str]:
    """Report contradictions between stored context fields and season dates."""
    issues = []
    if discipline and discipline not in DISCIPLINES:
        issues.append("unknown_discipline")
    if phase and phase not in PHASES:
        issues.append("unknown_phase")
    if phase and discipline and PHASE_DISCIPLINE.get(phase) != discipline:
        issues.append("phase_discipline_conflict")
    if edition is not None:
        first, last = edition_bounds(edition)
        if start < first or end > last:
            issues.append("dates_outside_edition")
    return issues


CANONICAL_WORDS = 3


# Canonical names the competition importer gives the seasons it creates. They are
# read once, by the schema migration, to record existing importer seasons; the
# running application never reads a name to decide context.
def canonical_context(name: str, start: date, end: date) -> tuple[int, str] | None:
    """Recognise an importer-created season whose name and dates agree.

    Returns:
        The edition and phase, or None for any other name or inconsistent dates.

    """
    words = name.strip().split()
    if len(words) != CANONICAL_WORDS or words[1].casefold() != "seizoen":
        return None
    prefix, years = words[0].casefold(), words[2]
    edition = edition_from_dates(start, end)
    if edition is None:
        return None
    expected = {
        "voor": (AUTUMN, str(edition)),
        "na": (SPRING, str(edition + 1)),
        "zaal": (INDOOR_PHASE, f"{edition}-{edition + 1}"),
        "veld": (FULL_SEASON, f"{edition}-{edition + 1}"),
    }.get(prefix)
    if expected is None or expected[1] != years:
        return None
    phase = expected[0]
    if phase == AUTUMN and start.month < EDITION_FIRST_MONTH:
        return None
    if phase == SPRING and start.month >= EDITION_FIRST_MONTH:
        return None
    return edition, phase
