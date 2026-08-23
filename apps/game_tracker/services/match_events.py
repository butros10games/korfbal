"""Append-only recording and ordering helpers for typed match records."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, cast
from uuid import UUID

from django.db import models, transaction
from django.db.models import Min

from apps.game_tracker.models import (
    Attack,
    MatchData,
    MatchEvent,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
    ShotEventDetail,
    SubstitutionEventDetail,
    Timeout,
)
from apps.game_tracker.services.match_event_context import (
    MatchEventContext,
    current_match_event_context,
)


TrackedModel = Attack | MatchPart | Pause | PlayerChange | Shot | Timeout


class _MatchDataBound(Protocol):
    match_data_id: str


_SOURCE_TYPES: dict[type[models.Model], str] = {
    Attack: "attack",
    MatchPart: "match_part",
    Pause: "pause",
    PlayerChange: "player_change",
    Shot: "shot",
    Timeout: "timeout",
}


def _json_value(value: object) -> object:
    """Convert a concrete model value into stable JSON data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal, Enum)):
        return str(value)
    return str(value)


def _snapshot(instance: TrackedModel) -> dict[str, Any]:
    """Capture concrete typed fields without traversing mutable relations."""
    return {
        field.attname: _json_value(getattr(instance, field.attname))
        for field in instance._meta.concrete_fields
        if field.attname != "match_data_id"
    }


def _effective_at(instance: TrackedModel, *, operation: str) -> datetime | None:
    """Return the real-world time represented by this event version."""
    if isinstance(instance, Pause):
        if operation != "created" and instance.end_time is not None:
            return instance.end_time
        return instance.start_time
    if isinstance(instance, MatchPart):
        if operation != "created" and instance.end_time is not None:
            return instance.end_time
        return instance.start_time
    if isinstance(instance, Timeout):
        pause = instance.pause
        return pause.start_time if pause is not None else None
    return instance.time


def _match_part(instance: TrackedModel) -> MatchPart | None:
    if isinstance(instance, MatchPart):
        return instance
    return instance.match_part


def _elapsed_ms(
    match_data: MatchData,
    instance: TrackedModel,
    effective_at: datetime | None,
) -> int | None:
    """Calculate a stable match-clock position for the event version."""
    part = _match_part(instance)
    if part is None or effective_at is None or part.start_time is None:
        return None

    pause_ms = 0
    intervals = Pause.objects.filter(
        match_data=match_data,
        match_part=part,
        start_time__lt=effective_at,
    ).values_list("start_time", "end_time")
    for start, end in intervals:
        if start is None:
            continue
        overlap_end = min(end or effective_at, effective_at)
        if overlap_end > start:
            pause_ms += int((overlap_end - start).total_seconds() * 1000)

    period_offset_ms = (part.part_number - 1) * match_data.part_length * 1000
    wall_ms = int((effective_at - part.start_time).total_seconds() * 1000)
    return max(0, period_offset_ms + wall_ms - pause_ms)


def _event_kind(source_type: str, instance: TrackedModel, operation: str) -> str:
    if operation == "deleted":
        return f"{source_type}.retracted"
    if isinstance(instance, Pause):
        if operation == "created":
            return "pause.started"
        if instance.end_time is not None and not instance.active:
            return "pause.ended"
    if isinstance(instance, MatchPart):
        if operation == "created":
            return "match_part.started"
        if instance.end_time is not None and not instance.active:
            return "match_part.ended"
    return f"{source_type}.{operation}"


def _snapshot_id(snapshot: dict[str, Any], field: str) -> str | None:
    value = snapshot.get(field)
    return str(value) if value is not None else None


def _create_typed_detail(
    event: MatchEvent,
    instance: TrackedModel,
    snapshot: dict[str, Any],
    context: MatchEventContext,
) -> None:
    """Persist canonical relational semantics for one immutable version."""
    if isinstance(instance, Shot):
        player_id = _snapshot_id(snapshot, "player_id")
        shooting_team_id = _snapshot_id(snapshot, "team_id")
        source_team_id = getattr(context.source_team, "pk", None)
        is_shooter = (
            context.source == "editor"
            or (source_team_id is not None and str(source_team_id) == shooting_team_id)
            or (source_team_id is None and bool(snapshot.get("for_team", True)))
        )
        ShotEventDetail.objects.create(
            event=event,
            shooting_team_id=shooting_team_id,
            shooter_id=player_id if is_shooter else None,
            defender_id=None if is_shooter else player_id,
            shot_type_id=_snapshot_id(snapshot, "shot_type_id"),
            outcome=(
                ShotEventDetail.OUTCOME_GOAL
                if snapshot.get("scored")
                else ShotEventDetail.OUTCOME_MISS
            ),
        )
        return

    if isinstance(instance, PlayerChange):
        player_group_id = _snapshot_id(snapshot, "player_group_id")
        team_id = instance.player_group.team_id if player_group_id is not None else None
        SubstitutionEventDetail.objects.create(
            event=event,
            team_id=team_id,
            player_out_id=_snapshot_id(snapshot, "player_out_id"),
            player_in_id=_snapshot_id(snapshot, "player_in_id"),
            player_group_id=player_group_id,
        )


def record_typed_match_event(
    instance: TrackedModel,
    *,
    operation: str,
) -> MatchEvent | None:
    """Append one versioned envelope for a typed tracker write."""
    source_type = _SOURCE_TYPES[type(instance)]
    match_data_id = cast(_MatchDataBound, instance).match_data_id
    with transaction.atomic():
        match_data = (
            MatchData.objects.select_for_update().filter(pk=match_data_id).first()
        )
        if match_data is None:
            return None

        previous = (
            MatchEvent.objects
            .filter(
                match_data=match_data,
                source_type=source_type,
                source_id=instance.pk,
                status=MatchEvent.STATUS_ACTIVE,
            )
            .order_by("-sequence")
            .first()
        )
        if previous is not None:
            previous.status = (
                MatchEvent.STATUS_RETRACTED
                if operation == "deleted"
                else MatchEvent.STATUS_SUPERSEDED
            )
            previous.save(update_fields=["status"])

        if operation == "deleted" and previous is not None:
            effective_at = previous.effective_at
            elapsed_ms = previous.elapsed_ms
            record_snapshot = previous.payload.get("record", {})
        else:
            effective_at = _effective_at(instance, operation=operation)
            elapsed_ms = _elapsed_ms(match_data, instance, effective_at)
            record_snapshot = _snapshot(instance)
        context = current_match_event_context()
        match_data.event_sequence += 1
        MatchData.objects.filter(pk=match_data.pk).update(
            event_sequence=match_data.event_sequence
        )
        event_kwargs: dict[str, Any] = {}
        if previous is not None:
            event_kwargs["logical_id"] = previous.logical_id
        event = MatchEvent.objects.create(
            match_data=match_data,
            sequence=match_data.event_sequence,
            match_part=_match_part(instance),
            kind=_event_kind(source_type, instance, operation),
            source_type=source_type,
            source_id=instance.pk,
            effective_at=effective_at,
            elapsed_ms=elapsed_ms,
            actor=(
                context.actor
                if getattr(context.actor, "is_authenticated", False)
                else None
            ),
            source_team=(
                context.source_team
                if getattr(context.source_team, "pk", None) is not None
                else None
            ),
            command_id=context.command_id,
            source=context.source,
            device_id=context.device_id,
            session_id=context.session_id,
            payload={
                "operation": operation,
                "record": record_snapshot,
            },
            supersedes=previous,
            **event_kwargs,
        )
        _create_typed_detail(event, instance, record_snapshot, context)
        return event


def event_root_sequences(
    match_data: MatchData,
) -> dict[tuple[str, str], int]:
    """Return the stable first-envelope sequence for each logical record."""
    return {
        (row["source_type"], str(row["source_id"])): row["root_sequence"]
        for row in MatchEvent.objects
        .filter(match_data=match_data)
        .values("source_type", "source_id")
        .annotate(root_sequence=Min("sequence"))
    }


def event_root_ids(match_data: MatchData) -> dict[tuple[str, str], str]:
    """Return the canonical logical id for each typed source record."""
    return {
        (event.source_type, str(event.source_id)): str(event.logical_id)
        for event in MatchEvent.objects
        .filter(match_data=match_data)
        .order_by("sequence")
        .only("source_type", "source_id", "logical_id")
    }
