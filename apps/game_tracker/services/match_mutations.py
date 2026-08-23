"""Shared transaction boundary for tracker and event-editor mutations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from django.db import transaction

from apps.game_tracker.models import MatchData
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource
from apps.game_tracker.services.lineup_projections import rebuild_match_projections
from apps.game_tracker.services.live_update_signal_control import (
    suppress_live_update_signals,
)
from apps.game_tracker.services.live_updates import record_match_change
from apps.game_tracker.services.match_event_context import match_event_context
from apps.game_tracker.services.match_timeline_payload import (
    build_match_events,
    build_match_shots,
)


_NO_OP_UNSET = object()


@contextmanager
def locked_match_mutation(match_data_id: object) -> Iterator[MatchData]:
    """Serialize a logical mutation by locking its aggregate root.

    Yields:
        The locked MatchData aggregate.

    """
    with transaction.atomic():
        yield MatchData.objects.select_for_update().get(pk=match_data_id)


def apply_editor_mutation[ResultT](
    *,
    match_data_id: object,
    actor: object | None,
    mutate: Callable[[MatchData], ResultT],
    no_op_result: object = _NO_OP_UNSET,
) -> tuple[MatchData, ResultT]:
    """Apply one editor correction with tracker-equivalent write semantics."""
    with locked_match_mutation(match_data_id) as match_data:
        before_events = {
            event["event_id"]: event for event in build_match_events(match_data)
        }
        before_shots = {
            shot["event_id"]: shot for shot in build_match_shots(match_data)
        }
        with (
            match_event_context(actor=actor, source="editor"),
            suppress_live_update_signals(),
        ):
            result = mutate(match_data)
            if no_op_result is not _NO_OP_UNSET and result is no_op_result:
                return match_data, result
            rebuild_match_projections(match_data)
        after_events = {
            event["event_id"]: event for event in build_match_events(match_data)
        }
        after_shots = {shot["event_id"]: shot for shot in build_match_shots(match_data)}
        changed_ids = {
            LiveResource.EVENTS: {
                event_id
                for event_id in before_events.keys() | after_events.keys()
                if before_events.get(event_id) != after_events.get(event_id)
            },
            LiveResource.SHOTS: {
                event_id
                for event_id in before_shots.keys() | after_shots.keys()
                if before_shots.get(event_id) != after_shots.get(event_id)
            },
        }
        record_match_change(
            match_data,
            resources=set(ALL_LIVE_RESOURCES),
            changed_ids=changed_ids,
        )
        return match_data, result
