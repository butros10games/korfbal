"""Finish estimates, separate from official playing time and final status.

2026/27 reference: https://www.knkv.nl/kennisbank/wedstrijdinformatie/
Unknown/stopped-clock formats retain the 90-minute elapsed-time heuristic.
"""

from datetime import datetime, timedelta
from typing import Any

from apps.competition.domain.match_rules import RuleContext, class_periods
from apps.schedule.domain.competition_context import edition_from_dates


FALLBACK_MINUTES = 90
BREAK_AND_REPORTING_MINUTES = 30


def expected_finish(row: dict[str, Any]) -> datetime:
    """Prefer supplied minutes, then unambiguous season-scoped class rules."""
    minutes = row.get("playing_time_minutes") or row.get("class_playing_time_minutes")
    if minutes is None and row.get("season__start_date"):
        minutes = class_playing_minutes(row)
    elapsed = minutes + BREAK_AND_REPORTING_MINUTES if minutes else FALLBACK_MINUTES
    return row["starts_at"] + timedelta(minutes=elapsed)


def class_playing_minutes(row: dict[str, Any]) -> int | None:
    """Do not infer age from team names or assume stopped-clock elapsed time.

    Rules are selected by the edition (July-June korfbal year), so a spring
    season starting in January uses the rules of the previous July's edition.
    """
    if row.get("pool__mapping_status") in {"conflict", "unresolved"}:
        return None
    prefix = "pool__competition_class__"
    periods = class_periods(
        RuleContext(
            edition=row_edition(row),
            discipline=row.get(prefix + "edition__discipline"),
            category=row.get(prefix + "category"),
            age_group=row.get(prefix + "age_group"),
            colour=row.get(prefix + "colour"),
            playing_format=row.get(prefix + "playing_format"),
            code=row.get(prefix + "code"),
            gender=row.get(prefix + "edition__gender"),
            team_kind=row.get(prefix + "team_kind"),
        )
    )
    return sum(periods) if periods else None


def row_edition(row: dict[str, Any]) -> int | None:
    """Return the stored edition, or one unambiguously implied by the dates."""
    if row.get("season__edition") is not None:
        return row["season__edition"]
    start, end = row.get("season__start_date"), row.get("season__end_date")
    if start is None:
        return None
    return edition_from_dates(start, end or start)
