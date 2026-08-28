"""Shared transaction boundary for tracker and event-editor mutations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from django.db import transaction

from apps.game_tracker.application.ports import MatchChangePublisher
from apps.game_tracker.models import MatchData
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource
from apps.game_tracker.services.lineup_projections import rebuild_match_projections
from apps.game_tracker.services.live_update_signal_control import (
    suppress_live_update_signals,
)
from apps.game_tracker.services.live_updates import record_match_change
from apps.game_tracker.services.match_event_context import match_event_context
from apps.game_tracker.services.match_timeline_payload import (
    build_match_timeline_payloads,
)


_NO_OP_UNSET = object()


@dataclass(frozen=True, slots=True)
class EditorMutationContext:
    """Shared metadata needed to apply an editor mutation."""

    match_data_id: object
    expected_revision: int
    actor: object | None
    publisher: MatchChangePublisher


@dataclass(slots=True)
class MatchRevisionConflictError(Exception):
    """The aggregate changed after the client loaded its editing snapshot."""

    expected_revision: int
    live_revision: int

    def __str__(self) -> str:
        """Return the provider-neutral conflict message."""
        return "The match changed while you were editing."


def require_match_revision(match_data: MatchData, *, expected_revision: int) -> None:
    """Reject a mutation based on a stale aggregate snapshot.

    Raises:
        MatchRevisionConflictError: If another write advanced the aggregate.

    """
    if match_data.live_revision != expected_revision:
        raise MatchRevisionConflictError(
            expected_revision=expected_revision,
            live_revision=match_data.live_revision,
        )


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
    context: EditorMutationContext,
    mutate: Callable[[MatchData], ResultT],
    no_op_result: object = _NO_OP_UNSET,
) -> tuple[MatchData, ResultT]:
    """Apply one editor correction with tracker-equivalent write semantics."""
    with locked_match_mutation(context.match_data_id) as match_data:
        require_match_revision(
            match_data,
            expected_revision=context.expected_revision,
        )
        before_event_rows, before_shot_rows = build_match_timeline_payloads(match_data)
        before_events = {event["event_id"]: event for event in before_event_rows}
        before_shots = {shot["event_id"]: shot for shot in before_shot_rows}
        with (
            match_event_context(actor=context.actor, source="editor"),
            suppress_live_update_signals(),
        ):
            result = mutate(match_data)
            if no_op_result is not _NO_OP_UNSET and result is no_op_result:
                return match_data, result
            rebuild_match_projections(match_data)
        after_event_rows, after_shot_rows = build_match_timeline_payloads(match_data)
        after_events = {event["event_id"]: event for event in after_event_rows}
        after_shots = {shot["event_id"]: shot for shot in after_shot_rows}
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
            publisher=context.publisher,
        )
        return match_data, result
