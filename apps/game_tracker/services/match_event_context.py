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


@dataclass(frozen=True, slots=True)
class MatchEventContext:
    """Attribution shared by all typed writes in one logical command."""

    actor: object | None = None
    source_team: object | None = None
    command_id: UUID | None = None
    source: str = "system"
    device_id: str = ""
    session_id: str = ""


_current_context: ContextVar[MatchEventContext | None] = ContextVar(
    "match_event_context",
    default=None,
)
_deleting_match_data_ids: ContextVar[frozenset[str] | None] = ContextVar(
    "deleting_match_data_ids",
    default=None,
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
        )
    )
    try:
        yield
    finally:
        _current_context.reset(token)
