"""Minutes-played computation based on match timeline data.

This mirrors the role-timeline reconstruction used by match impact.

Important:
    Minutes-played must be computed asynchronously (Celery) and persisted to
    `PlayerMatchMinutes`. Request handlers should only read persisted rows and
    must not trigger recomputation.

"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
import logging

from django.db import transaction

from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    Pause,
    PlayerGroup,
    PlayerMatchMinutes,
)
from apps.game_tracker.models.player_match_minutes import LATEST_MATCH_MINUTES_VERSION
from apps.game_tracker.services.match_impact import (
    build_match_player_role_timeline,
    compute_match_end_minutes,
)
from apps.schedule.api.match_events_payload import build_match_events, build_match_shots


logger = logging.getLogger(__name__)


def _collect_known_player_ids(
    *,
    groups: list[PlayerGroup],
    events: list[dict[str, object]],
) -> set[str]:
    known_player_ids: set[str] = set()
    for group in groups:
        for player in group.players.all():
            pid = str(getattr(player, "id_uuid", "") or "").strip()
            if pid:
                known_player_ids.add(pid)

    # Substitution payloads may reference IDs not present in groups.
    for event in events:
        for key in ("player_in_id", "player_out_id", "player_id"):
            raw = event.get(key)
            if isinstance(raw, str):
                pid = raw.strip()
            elif raw is None:
                pid = ""
            else:
                pid = str(raw).strip()
            if pid:
                known_player_ids.add(pid)

    return known_player_ids


def _sum_interval_minutes(
    *,
    items: Iterable[object],
    match_end_minutes: float,
) -> float:
    total = 0.0
    for item in items:
        start = float(getattr(item, "start", 0.0) or 0.0)
        end = float(getattr(item, "end", 0.0) or 0.0)
        start = max(0.0, min(start, match_end_minutes))
        end = max(0.0, min(end, match_end_minutes))
        if end > start:
            total += end - start
    return total


def _sum_on_field_minutes(*, intervals: object, match_end_minutes: float) -> float:
    return (
        _sum_interval_minutes(
            items=getattr(intervals, "aanval", ()),
            match_end_minutes=match_end_minutes,
        )
        + _sum_interval_minutes(
            items=getattr(intervals, "verdediging", ()),
            match_end_minutes=match_end_minutes,
        )
        + _sum_interval_minutes(
            items=getattr(intervals, "unknown", ()),
            match_end_minutes=match_end_minutes,
        )
    )


def _expected_match_end_minutes(match_data: MatchData) -> float:
    """Return the expected match length from MatchData settings.

    This ignores pauses and intermissions, but provides a much better fallback
    than the timeline-derived default of 1 minute when no event timestamps are
    available.
    """
    parts = float(getattr(match_data, "parts", 0) or 0)
    part_length_seconds = float(getattr(match_data, "part_length", 0) or 0)
    expected = (parts * part_length_seconds) / 60.0
    return max(1.0, expected)


def _match_end_minutes_from_match_parts(match_data: MatchData) -> float | None:
    """Compute match end minutes from recorded MatchPart times.

    We subtract completed pause durations to align with the minute formatting
    used in the match payload builders.
    """
    parts = list(
        MatchPart.objects
        .filter(match_data=match_data, end_time__isnull=False)
        .only("start_time", "end_time")
        .order_by("part_number", "start_time")
    )
    if not parts:
        return None

    part_seconds = 0.0
    for part in parts:
        if not part.start_time or not part.end_time:
            continue
        delta = (part.end_time - part.start_time).total_seconds()
        if delta > 0:
            part_seconds += delta

    if part_seconds <= 0:
        return None

    pause_seconds = 0.0
    for pause in Pause.objects.filter(
        match_data=match_data,
        active=False,
        start_time__isnull=False,
        end_time__isnull=False,
        match_part__in=parts,
    ).only("start_time", "end_time"):
        pause_seconds += pause.length().total_seconds()

    return max(1.0, (max(0.0, part_seconds - pause_seconds) / 60.0))


def compute_minutes_by_player_id(*, match_data: MatchData) -> dict[str, float]:
    """Compute minutes played for each player in a match.

    Returns a mapping of player UUID (string) -> minutes played (float).
    """
    events = build_match_events(match_data)
    shots = build_match_shots(match_data)

    match_end_minutes = compute_match_end_minutes(events=events, shots=shots)

    # The timeline payload builders can return "?" for shots without part/time.
    # When all events/shots are missing timestamps, the JS-parity end-minute
    # falls back to 1.0, which makes all players appear to have ~0-1 minutes.
    # For minutes-played we prefer a match-length fallback.
    match_end_minutes = max(match_end_minutes, _expected_match_end_minutes(match_data))
    from_parts = _match_end_minutes_from_match_parts(match_data)
    if from_parts is not None:
        match_end_minutes = max(match_end_minutes, from_parts)

    groups = list(
        PlayerGroup.objects
        .select_related("starting_type", "team")
        .prefetch_related("players")
        .filter(match_data=match_data)
    )

    known_player_ids = _collect_known_player_ids(groups=groups, events=events)

    role_intervals_by_id = build_match_player_role_timeline(
        known_player_ids=sorted(known_player_ids),
        groups=groups,
        events=events,
        match_end_minutes=match_end_minutes,
    )

    minutes_by_player_id: dict[str, float] = {}
    for pid, intervals in role_intervals_by_id.items():
        minutes = _sum_on_field_minutes(
            intervals=intervals,
            match_end_minutes=match_end_minutes,
        )
        minutes_by_player_id[pid] = round(minutes, 2)

    return minutes_by_player_id


def persist_match_minutes(*, match_data: MatchData) -> int:
    """Compute + upsert minutes played rows for a match.

    Returns number of rows written.
    """
    minutes_by_player_id = compute_minutes_by_player_id(match_data=match_data)
    if not minutes_by_player_id:
        return 0

    rows_written = 0

    # Keep writes consistent if multiple signals fire in a short time.
    with transaction.atomic():
        for player_id, minutes in minutes_by_player_id.items():
            if minutes <= 0:
                continue
            PlayerMatchMinutes.objects.update_or_create(
                match_data=match_data,
                player_id=player_id,
                algorithm_version=LATEST_MATCH_MINUTES_VERSION,
                defaults={"minutes_played": Decimal(str(minutes))},
            )
            rows_written += 1

    logger.info(
        "Persisted match minutes for %s (%s rows)",
        match_data.id_uuid,
        rows_written,
    )
    return rows_written
