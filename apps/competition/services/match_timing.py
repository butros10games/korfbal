"""Read playing minutes from the captured v8 match-details contract."""

from datetime import datetime
from typing import Any

from apps.competition.models import Match
from apps.competition.services.cups import import_cup_fixture
from apps.schedule.models import Season


MAX_PLAYING_MINUTES = 180


def playing_minutes(data: dict[str, Any]) -> int | None:
    """Accept observed event resolutions; NONE also requires matching period totals."""
    if data.get("EventTimeResolution") not in {"MINUTE", "NONE"}:
        return None
    value = data.get("Duration")
    if type(value) is int and 0 < value <= MAX_PLAYING_MINUTES:
        return value
    return None


def import_playing_time(
    match: Match, data: dict[str, Any], observed_at: datetime
) -> bool:
    """Retain regulation minutes and possible periods, not elapsed match duration."""
    if match.playing_time_observed_at and match.playing_time_observed_at > observed_at:
        return True
    value = playing_minutes(data)
    periods = data.get("MatchPeriod")
    if value is None or not isinstance(periods, list) or not periods:
        return False
    if any(
        not isinstance(period, dict)
        or type(period.get("PlayTime")) is not int
        or not 0 <= period["PlayTime"] <= MAX_PLAYING_MINUTES
        or (
            period["PlayTime"] == 0
            and period.get("Description", "") != "Strafworpserie"
        )
        or not isinstance(period.get("Description"), str)
        for period in periods
    ):
        return False
    if not any(period["PlayTime"] > 0 for period in periods):
        return False
    if (
        data.get("EventTimeResolution") == "NONE"
        and sum(
            period["PlayTime"]
            for period in periods
            if period["Description"] not in {"1e verlenging", "2e verlenging"}
        )
        != value
    ):
        return False
    import_cup_fixture(match)
    match.playing_time_minutes = value
    match.playing_time_observed_at = observed_at
    match.match_periods = [
        {key: period[key] for key in ("Description", "PlayTime")} for period in periods
    ]
    # Timing metadata is not a score correction or native schedule change.
    match.save(
        update_fields=(
            "playing_time_minutes",
            "playing_time_observed_at",
            "match_periods",
        )
    )

    return True


def import_timing_details(
    season: Season, source_id: str, data: dict[str, Any], observed_at: datetime
) -> None:
    """Import a duration-only sample without enabling roster or lineup imports.

    Raises:
        ValueError: The response does not identify the requested match.

    """
    if str(data.get("PublicMatchId", "")) != source_id:
        raise ValueError("Unrecognized match timing")
    match = Match.objects.get(season=season, external_id=source_id)
    if not import_playing_time(match, data, observed_at):
        raise ValueError("Missing or unsupported match duration/periods")
