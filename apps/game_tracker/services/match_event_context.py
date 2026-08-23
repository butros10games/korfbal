"""Request-local attribution for append-only match events."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class MatchEventClient:
    """Stable client attribution supplied by an online or offline tracker."""

    device_id: str = ""
    session_id: str = ""
    client_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class MatchEventContext:
    """Attribution shared by all typed writes in one logical command."""

    actor: object | None = None
    source_team: object | None = None
    command_id: UUID | None = None
    source: str = "system"
    device_id: str = ""
    session_id: str = ""
    client_sequence: int | None = None


_current_context: ContextVar[MatchEventContext | None] = ContextVar(
    "match_event_context",
    default=None,
)
_deleting_match_data_ids: ContextVar[frozenset[str] | None] = ContextVar(
    "deleting_match_data_ids",
    default=None,
)
_recording_suppressed: ContextVar[bool] = ContextVar(
    "match_event_recording_suppressed",
    default=False,
)


def current_match_event_context() -> MatchEventContext:
    """Return attribution for the current tracker write."""
    return _current_context.get() or MatchEventContext()


def mark_match_data_deleting(match_data_id: object) -> None:
    """Suppress child retraction events during a whole-match cascade."""
    current = _deleting_match_data_ids.get() or frozenset()
    _deleting_match_data_ids.set(current | {str(match_data_id)})


def unmark_match_data_deleting(match_data_id: object) -> None:
    """Clear whole-match deletion suppression for one aggregate."""
    current = _deleting_match_data_ids.get() or frozenset()
    _deleting_match_data_ids.set(current - {str(match_data_id)})


def match_data_is_deleting(match_data_id: object) -> bool:
    """Return whether the aggregate is being removed by a cascade."""
    return str(match_data_id) in (_deleting_match_data_ids.get() or frozenset())


def match_event_recording_is_suppressed() -> bool:
    """Return whether projection maintenance must bypass event capture."""
    return _recording_suppressed.get()


@contextmanager
def suppress_match_event_recording() -> Iterator[None]:
    """Prevent replayed projection writes from creating new domain events."""
    token = _recording_suppressed.set(True)
    try:
        yield
    finally:
        _recording_suppressed.reset(token)


@contextmanager
def match_event_context(
    *,
    actor: object | None = None,
    source_team: object | None = None,
    command_id: UUID | None = None,
    source: str = "system",
    client: MatchEventClient | None = None,
) -> Iterator[None]:
    """Attach actor, team, and command identity to nested model signals."""
    token = _current_context.set(
        MatchEventContext(
            actor=actor,
            source_team=source_team,
            command_id=command_id,
            source=source,
            device_id=client.device_id if client else "",
            session_id=client.session_id if client else "",
            client_sequence=client.client_sequence if client else None,
        )
    )
    try:
        yield
    finally:
        _current_context.reset(token)
