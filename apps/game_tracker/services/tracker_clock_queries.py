"""Shared aggregate clock reads for private and public tracker snapshots."""

from datetime import UTC, datetime
from typing import Any

from django.db import models

from apps.game_tracker.models import MatchData, MatchPart, Pause


def read_clock_state(
    match_data: MatchData,
    match_part: MatchPart | None,
) -> tuple[bool, dict[str, Any]]:
    """Read pause controls and elapsed pause time in one aggregate query."""
    if match_part is None:
        return True, {
            "type": "deactivated",
            "match_data_id": str(match_data.id_uuid),
        }

    pauses = Pause.objects.filter(
        match_data=match_data,
        match_part=match_part,
    ).aggregate(
        # Constraints guarantee at most one active pause with a non-null start.
        paused_since=models.Min("start_time", filter=models.Q(active=True)),
        completed_duration=models.Sum(
            models.F("end_time") - models.F("start_time"),
            filter=models.Q(active=False),
        ),
    )
    paused_since = pauses["paused_since"]
    completed_duration = pauses["completed_duration"]
    base: dict[str, Any] = {
        "match_data_id": str(match_data.id_uuid),
        "time": match_part.start_time.isoformat(),
        "length": match_data.part_length,
        "pause_length": (
            completed_duration.total_seconds() if completed_duration else 0
        ),
        "server_time": datetime.now(UTC).isoformat(),
    }
    if paused_since is not None:
        return True, {
            **base,
            "type": "pause",
            "calc_to": paused_since.isoformat(),
        }
    return match_data.status != "active", {**base, "type": "active"}
