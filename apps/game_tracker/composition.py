"""Production composition root for match-tracker use cases."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import partial
from typing import Any

from django.utils import timezone

from apps.game_tracker.adapters.outbound.runtime import (
    CeleryTrackerJobDispatcher,
    ChannelsMatchChangePublisher,
)
from apps.game_tracker.application.ports import TrackerRuntime
from apps.game_tracker.models import MatchData
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource
from apps.game_tracker.services.event_editor import (
    apply_event_editor_command as _apply_event_editor_command,
)
from apps.game_tracker.services.live_updates import (
    record_match_change as _record_change,
)
from apps.game_tracker.services.player_designation import (
    apply_player_designation as _apply_player_designation,
)
from apps.game_tracker.services.tracker_http import execute_tracker_command
from apps.schedule.models import Match
from apps.team.models import Team


change_publisher = ChannelsMatchChangePublisher()
tracker_jobs = CeleryTrackerJobDispatcher()
tracker_runtime = TrackerRuntime(
    now=timezone.now,
    jobs=tracker_jobs,
    publisher=change_publisher,
)
apply_event_editor_command = partial(
    _apply_event_editor_command,
    publisher=change_publisher,
)
apply_player_designation = partial(
    _apply_player_designation,
    publisher=change_publisher,
)


def apply_tracker_command(
    match: Match,
    *,
    team: Team,
    payload: dict[str, Any],
    actor: object | None = None,
) -> dict[str, Any]:
    """Apply a tracker command with production runtime adapters."""
    return execute_tracker_command(
        match,
        team=team,
        payload=payload,
        actor=actor,
        runtime=tracker_runtime,
    )


def record_match_change(
    match_data: MatchData,
    *,
    resources: Iterable[LiveResource] = ALL_LIVE_RESOURCES,
    changed_ids: Mapping[LiveResource, Iterable[str]] | None = None,
) -> int:
    """Record and publish a match change with production adapters."""
    return _record_change(
        match_data,
        resources=resources,
        changed_ids=changed_ids,
        publisher=change_publisher,
    )


# These adapters persist intent within the caller's transaction.
schedule_match_impact_recompute = tracker_jobs.recompute_impacts
schedule_match_minutes_recompute = tracker_jobs.recompute_minutes
