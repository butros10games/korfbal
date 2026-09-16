"""Production composition root for match-tracker use cases."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import partial
from typing import Any

from django.utils import timezone

from apps.game_tracker.adapters.outbound.published_live_store import (
    SharedPublishedLiveStore,
)
from apps.game_tracker.adapters.outbound.runtime import (
    CeleryTrackerJobDispatcher,
    ChannelsMatchChangePublisher,
)
from apps.game_tracker.adapters.outbound.shared_compact import SharedCompactStore
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
from apps.game_tracker.services.public_live import (
    publish_public_live,
    read_cached_public_live,
    read_published_live,
)
from apps.game_tracker.services.public_match_reads import (
    PUBLIC_MATCH_RESOURCES,
    publish_public_match_reads,
    read_public_match_resource,
    read_public_match_updates,
    resource_key,
)
from apps.game_tracker.services.tracker_http import execute_tracker_command
from apps.schedule.models import Match
from apps.team.models import Team


published_live_store = SharedPublishedLiveStore()
published_match_store = SharedPublishedLiveStore("public-match:published:v1")


def invalidate_public_match_reads(match_id: str, revision: int) -> None:
    """Fence every public resource following a committed edit or deletion."""
    for resource in PUBLIC_MATCH_RESOURCES:
        published_match_store.invalidate(resource_key(match_id, resource), revision)


prepare_public_match_reads = partial(
    publish_public_match_reads, store=published_match_store
)
read_public_match = partial(read_public_match_resource, store=published_match_store)

change_publisher = ChannelsMatchChangePublisher(
    published_live_store,
    lambda match_id: publish_public_live(match_id=match_id, store=published_live_store),
    lambda match_id: prepare_public_match_reads(match_id=match_id),
    invalidate_public_match_reads,
    partial(read_public_match_updates, store=published_match_store),
)
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


read_cached_live = partial(read_cached_public_live, store=published_live_store)
read_public_live = partial(read_published_live, store=published_live_store)
publish_public_live_snapshot = partial(publish_public_live, store=published_live_store)


shared_compact_store = SharedCompactStore()
