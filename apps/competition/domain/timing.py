"""Finish estimates, separate from official playing time and final status.

2026/27 reference: https://www.knkv.nl/kennisbank/wedstrijdinformatie/
Unknown/stopped-clock formats retain the 90-minute elapsed-time heuristic.
"""

from datetime import datetime, timedelta
from typing import Any


RULE_YEAR = 2026
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
    """Do not infer age from team names or assume stopped-clock elapsed time."""
    if row["season__start_date"].year != RULE_YEAR or row.get(
        "pool__mapping_status"
    ) in {"conflict", "unresolved"}:
        return None
    prefix = "pool__competition_class__"
    category = row.get(prefix + "category")
    age = row.get(prefix + "age_group")
    colour = row.get(prefix + "colour")
    form = row.get(prefix + "playing_format")
    discipline = row.get(prefix + "edition__discipline")
    if category == "b":
        if form == "four" and colour in {"red", "orange", "yellow", "green", "blue"}:
            return 40
        if form == "eight":
            return {"orange": 50, "yellow": 50, "red": 60}.get(
                colour, 60 if age == "senior" else None
            )
    if category == "a":
        if age == "U15" and discipline in {"indoor", "outdoor"}:
            return 50
        if age in {"senior", "U19", "U17"} and discipline == "outdoor":
            return 60
    return None
