"""Consistent, non-blocking read models for public match timelines."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from django.db import connection, models, transaction

from apps.game_tracker.models import MatchData
from apps.game_tracker.models.match_part import MatchPart
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.services.live_updates import summarize_match_changes
from apps.game_tracker.services.match_events import build_match_event_history
from apps.game_tracker.services.match_timeline_payload import (
    build_match_events,
    build_match_shots,
    load_match_timeline_context,
)


MATCH_TIMELINE_IDENTITY_VERSION = 3
type TimelineItem = dict[str, Any]
type TimelineMode = Literal["full", "delta"]


@contextmanager
def consistent_timeline_read() -> Iterator[None]:
    """Keep related selects on one MVCC snapshot without locking writers."""
    is_outermost = not connection.in_atomic_block
    with transaction.atomic():
        if is_outermost and connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
        yield


@dataclass(frozen=True, slots=True)
class TimelineWindow:
    """Full or incremental items at one durable match revision."""

    mode: TimelineMode
    items: tuple[TimelineItem, ...]
    base_revision: int | None = None
    upsert: tuple[TimelineItem, ...] = ()
    deleted_ids: tuple[str, ...] = ()
    order: tuple[str, ...] = ()

    def to_payload(self, *, collection_key: str) -> dict[str, object]:
        """Adapt the neutral window to the stable HTTP collection keys."""
        if self.mode == "full":
            return {"mode": "full", collection_key: list(self.items)}
        return {
            "mode": "delta",
            "base_revision": self.base_revision,
            "upsert": list(self.upsert),
            "deleted_ids": list(self.deleted_ids),
            "order": list(self.order),
        }


@dataclass(frozen=True, slots=True)
class MatchEventsSnapshot:
    """Read model for the match events endpoint."""

    live_revision: int
    home_team_id: str
    match_parts: tuple[dict[str, object], ...]
    status: str
    window: TimelineWindow

    def to_payload(self) -> dict[str, object]:
        """Return the existing version-three events response contract."""
        return {
            "identity_version": MATCH_TIMELINE_IDENTITY_VERSION,
            "home_team_id": self.home_team_id,
            "match_parts": list(self.match_parts),
            "status": self.status,
            "live_revision": self.live_revision,
            **self.window.to_payload(collection_key="events"),
        }


@dataclass(frozen=True, slots=True)
class MatchShotsSnapshot:
    """Read model for the match shots endpoint."""

    live_revision: int
    home_team_id: str
    away_team_id: str
    status: str
    window: TimelineWindow

    def to_payload(self) -> dict[str, object]:
        """Return the existing version-three shots response contract."""
        return {
            "identity_version": MATCH_TIMELINE_IDENTITY_VERSION,
            "home_team_id": self.home_team_id,
            "away_team_id": self.away_team_id,
            "status": self.status,
            "live_revision": self.live_revision,
            **self.window.to_payload(collection_key="shots"),
        }


@dataclass(frozen=True, slots=True)
class MatchEventHistorySnapshot:
    """Immutable audit-history response model."""

    events: tuple[TimelineItem, ...]

    def to_payload(self) -> dict[str, object]:
        """Return the existing event-history response contract."""
        return {"events": list(self.events)}


def _window_for(
    *,
    match_data: MatchData,
    items: list[TimelineItem],
    resource: LiveResource,
    since_revision: int | None,
    current_identity: bool,
) -> TimelineWindow:
    if since_revision is None or not current_identity:
        return TimelineWindow(mode="full", items=tuple(items))

    summary = summarize_match_changes(
        match_data,
        since_revision=since_revision,
    )
    can_send_delta = summary.history_complete and (
        resource not in summary.resources or resource in summary.complete_id_resources
    )
    if not can_send_delta:
        return TimelineWindow(mode="full", items=tuple(items))

    changed_ids = summary.changed_ids.get(resource, frozenset())
    current_ids = {str(item["event_id"]) for item in items}
    return TimelineWindow(
        mode="delta",
        items=(),
        base_revision=since_revision,
        upsert=tuple(item for item in items if str(item["event_id"]) in changed_ids),
        deleted_ids=tuple(sorted(changed_ids - current_ids)),
        order=tuple(str(item["event_id"]) for item in items),
    )


def _match_part_payload(part: MatchPart) -> dict[str, object]:
    return {
        "id_uuid": str(part.id_uuid),
        "part_number": part.part_number,
        "start_time": part.start_time.isoformat() if part.start_time else None,
        "end_time": part.end_time.isoformat() if part.end_time else None,
        "active": bool(part.active),
    }


def read_match_events(
    *,
    match_data_id: object,
    since_revision: int | None,
    current_identity: bool,
) -> MatchEventsSnapshot:
    """Read one consistent event timeline without taking an aggregate write lock."""
    with consistent_timeline_read():
        match_data = (
            MatchData.objects
            .select_related("match_link")
            .fetch_mode(models.FETCH_RAISE)
            .get(pk=match_data_id)
        )
        if match_data.status == "upcoming":
            match_parts: tuple[dict[str, object], ...] = ()
            items: list[TimelineItem] = []
        else:
            context = load_match_timeline_context(match_data)
            match_parts = tuple(
                _match_part_payload(part) for part in context.match_parts
            )
            items = build_match_events(match_data, context=context)
        return MatchEventsSnapshot(
            live_revision=match_data.live_revision,
            home_team_id=str(match_data.match_link.home_team_id),
            match_parts=match_parts,
            status=match_data.status,
            window=_window_for(
                match_data=match_data,
                items=items,
                resource=LiveResource.EVENTS,
                since_revision=since_revision,
                current_identity=current_identity,
            ),
        )


def read_match_shots(
    *,
    match_data_id: object,
    since_revision: int | None,
    current_identity: bool,
) -> MatchShotsSnapshot:
    """Read one consistent shot timeline without taking an aggregate write lock."""
    with consistent_timeline_read():
        match_data = (
            MatchData.objects
            .select_related("match_link")
            .fetch_mode(models.FETCH_RAISE)
            .get(pk=match_data_id)
        )
        items = [] if match_data.status == "upcoming" else build_match_shots(match_data)
        return MatchShotsSnapshot(
            live_revision=match_data.live_revision,
            home_team_id=str(match_data.match_link.home_team_id),
            away_team_id=str(match_data.match_link.away_team_id),
            status=match_data.status,
            window=_window_for(
                match_data=match_data,
                items=items,
                resource=LiveResource.SHOTS,
                since_revision=since_revision,
                current_identity=current_identity,
            ),
        )


def read_match_event_history(*, match_data_id: object) -> MatchEventHistorySnapshot:
    """Read the append-only event audit stream from one database snapshot."""
    with consistent_timeline_read():
        match_data = MatchData.objects.get(pk=match_data_id)
        return MatchEventHistorySnapshot(
            events=tuple(build_match_event_history(match_data))
        )
