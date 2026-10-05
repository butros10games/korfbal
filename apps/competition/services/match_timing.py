"""Read playing minutes from the captured v8 match-details contract."""

from datetime import datetime
from typing import Any

from django.db import transaction

from apps.competition.models import Match
from apps.competition.services.cups import import_cup_fixture
from apps.competition.services.match_details import metadata_context, observe_component
from apps.competition.services.match_rules import sync_tracker_rules
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
    stored = [
        {key: period[key] for key in ("Description", "PlayTime")} for period in periods
    ]
    changed = (match.playing_time_minutes, match.match_periods) != (value, stored)
    match.playing_time_minutes = value
    match.playing_time_observed_at = observed_at
    match.match_periods = stored
    observe_component(match, "match_timing", observed_at, available=True)
    # Timing metadata is not a score correction or native schedule change.
    match.save(
        update_fields=(
            "playing_time_minutes",
            "playing_time_observed_at",
            "match_periods",
            "metadata_observations",
        )
    )
    if changed and match.local_match_id is not None:
        # Timing can arrive after publication: untouched trackers adopt it now,
        # tracked ones keep it for review. No source timestamp changes.
        with transaction.atomic():
            sync_tracker_rules(match)
    return True


@transaction.atomic
def import_timing_details(
    season: Season,
    source_id: str,
    data: dict[str, Any],
    observed_at: datetime,
    *,
    expected_context: str | None = None,
) -> None:
    """Import a duration-only sample without enabling roster or lineup imports.

    Raises:
        ValueError: The response does not identify the requested match.

    """
    if source_id.startswith(("archive:", "ds:")):
        raise ValueError("Unsupported match timing identity")
    if str(data.get("PublicMatchId", "")) != source_id:
        raise ValueError("Unrecognized match timing")
    match = Match.objects.select_for_update(no_key=True).get(
        season=season, external_id=source_id
    )
    if (
        match.playing_time_observed_at and match.playing_time_observed_at > observed_at
    ) or (
        expected_context is not None
        and expected_context != metadata_context(match, "match_timing")
    ):
        return
    if not data.get("Duration") and not data.get("MatchPeriod"):
        match.playing_time_observed_at = observed_at
        observe_component(match, "match_timing", observed_at, available=False)
        match.save(update_fields=("playing_time_observed_at", "metadata_observations"))
        return
    if not import_playing_time(match, data, observed_at):
        raise ValueError("Missing or unsupported match duration/periods")
