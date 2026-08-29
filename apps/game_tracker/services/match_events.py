"""Append-only recording and ordering helpers for typed match records."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, cast
from uuid import UUID

from django.db import models, transaction
from django.db.models import OuterRef, QuerySet, Subquery

from apps.game_tracker.models import (
    Attack,
    MatchData,
    MatchEvent,
    MatchEventObservation,
    MatchPart,
    Pause,
    PlayerChange,
    PossessionChange,
    PossessionChangeEventDetail,
    Shot,
    ShotEventDetail,
    SubstitutionEventDetail,
    Timeout,
)
from apps.game_tracker.services.match_event_context import (
    MatchEventContext,
    current_match_event_context,
)


TrackedModel = (
    Attack | MatchPart | Pause | PlayerChange | PossessionChange | Shot | Timeout
)


class _MatchDataBound(Protocol):
    match_data_id: str


_SOURCE_TYPES: dict[type[models.Model], str] = {
    Attack: "attack",
    MatchPart: "match_part",
    Pause: "pause",
    PlayerChange: "player_change",
    PossessionChange: "possession_change",
    Shot: "shot",
    Timeout: "timeout",
}


def latest_match_events(
    match_data: MatchData,
    *,
    source_types: set[str] | None = None,
) -> QuerySet[MatchEvent]:
    """Return the immutable latest version of each logical event root."""
    latest_id = (
        MatchEvent.objects
        .filter(
            match_data=match_data,
            logical_id=OuterRef("logical_id"),
        )
        .order_by("-sequence")
        .values("pk")[:1]
    )
    events = MatchEvent.objects.filter(
        match_data=match_data,
        pk=Subquery(latest_id),
    )
    if source_types is not None:
        events = events.filter(source_type__in=source_types)
    return events


def active_match_events(
    match_data: MatchData,
    *,
    source_types: set[str] | None = None,
) -> QuerySet[MatchEvent]:
    """Return latest logical versions whose final fact is not a retraction."""
    return latest_match_events(
        match_data,
        source_types=source_types,
    ).exclude(kind__endswith=".retracted")


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


def _match_part(instance: TrackedModel | MatchEvent) -> MatchPart | None:
    if isinstance(instance, MatchPart):
        return instance
    if isinstance(instance, MatchEvent):
        if instance.period_id is None:
            return None
        return MatchPart.objects.filter(pk=instance.period_id).first()
    return instance.match_part


def _elapsed_ms(
    match_data: MatchData,
    instance: TrackedModel | MatchEvent,
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
            source_team_id is not None and str(source_team_id) == shooting_team_id
        ) or (source_team_id is None and bool(snapshot.get("for_team", True)))
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
        return

    if isinstance(instance, PossessionChange):
        PossessionChangeEventDetail.objects.create(
            event=event,
            team_id=_snapshot_id(snapshot, "team_id"),
            player_id=_snapshot_id(snapshot, "player_id"),
            kind=str(snapshot.get("kind") or ""),
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
            )
            .order_by("-sequence")
            .first()
        )

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
            period_id=(
                part.pk if (part := _match_part(instance)) is not None else None
            ),
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
        MatchEventObservation.objects.create(
            match_data=match_data,
            event=event,
            command_id=context.command_id,
            reporting_team=(
                context.source_team
                if getattr(context.source_team, "pk", None) is not None
                else None
            ),
            actor=(
                context.actor
                if getattr(context.actor, "is_authenticated", False)
                else None
            ),
            source=context.source,
            device_id=context.device_id,
            session_id=context.session_id,
            client_sequence=context.client_sequence,
            effective_at=effective_at,
            elapsed_ms=elapsed_ms,
            origin=MatchEventObservation.ORIGIN_CANONICAL,
            payload={
                "kind": event.kind,
                "record": record_snapshot,
            },
        )
        return event


def event_root_sequences(
    match_data: MatchData,
) -> dict[tuple[str, str], int]:
    """Return the stable first-envelope sequence for each logical record."""
    sequences, _logical_ids = event_root_metadata(match_data)
    return sequences


def event_root_ids(match_data: MatchData) -> dict[tuple[str, str], str]:
    """Return the canonical logical id for each typed source record."""
    _sequences, logical_ids = event_root_metadata(match_data)
    return logical_ids


def event_root_metadata(
    match_data: MatchData,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], str]]:
    """Load ordering and logical identities for typed records in one query."""
    sequences: dict[tuple[str, str], int] = {}
    logical_ids: dict[tuple[str, str], str] = {}
    rows = (
        MatchEvent.objects
        .filter(match_data=match_data)
        .order_by("sequence")
        .values_list("source_type", "source_id", "sequence", "logical_id")
    )
    for source_type, source_id, sequence, logical_id in rows:
        key = (source_type, str(source_id))
        sequences.setdefault(key, sequence)
        logical_ids[key] = str(logical_id)
    return sequences, logical_ids


def logical_event_id(
    match_data: MatchData,
    *,
    source_type: str,
    source_id: object,
) -> str:
    """Resolve a typed projection id to its stable logical event identity."""
    if source_type == "pause":
        timeout_source_id = (
            Timeout.objects
            .filter(
                match_data=match_data,
                pause_id=source_id,
            )
            .values_list("pk", flat=True)
            .first()
        )
        if timeout_source_id is not None:
            source_type = "timeout"
            source_id = timeout_source_id
    value = (
        MatchEvent.objects
        .filter(
            match_data=match_data,
            source_type=source_type,
            source_id=source_id,
        )
        .order_by("sequence")
        .values_list("logical_id", flat=True)
        .first()
    )
    return str(value) if value else str(source_id)


def build_match_event_history(match_data: MatchData) -> list[dict[str, Any]]:
    """Return the complete ordered audit stream, including inactive versions."""
    events = list(
        MatchEvent.objects
        .filter(match_data=match_data)
        .select_related(
            "shot_detail",
            "substitution_detail",
            "possession_change_detail",
        )
        .prefetch_related("observations")
        .order_by("sequence")
        .fetch_mode(models.FETCH_RAISE)
    )
    latest_sequence_by_logical_id = {
        event.logical_id: event.sequence for event in events
    }
    history: list[dict[str, Any]] = []
    for event in events:
        detail: dict[str, object] | None = None
        if hasattr(event, "shot_detail"):
            shot = event.shot_detail
            detail = {
                "shooting_team_id": (
                    str(shot.shooting_team_id) if shot.shooting_team_id else None
                ),
                "shooter_id": str(shot.shooter_id) if shot.shooter_id else None,
                "defender_id": str(shot.defender_id) if shot.defender_id else None,
                "shot_type_id": str(shot.shot_type_id) if shot.shot_type_id else None,
                "outcome": shot.outcome,
            }
        elif hasattr(event, "substitution_detail"):
            substitution = event.substitution_detail
            detail = {
                "team_id": (
                    str(substitution.team_id) if substitution.team_id else None
                ),
                "player_out_id": (
                    str(substitution.player_out_id)
                    if substitution.player_out_id
                    else None
                ),
                "player_in_id": (
                    str(substitution.player_in_id)
                    if substitution.player_in_id
                    else None
                ),
                "player_group_id": (
                    str(substitution.player_group_id)
                    if substitution.player_group_id
                    else None
                ),
            }
        elif hasattr(event, "possession_change_detail"):
            possession_change = event.possession_change_detail
            detail = {
                "team_id": (
                    str(possession_change.team_id)
                    if possession_change.team_id
                    else None
                ),
                "player_id": (
                    str(possession_change.player_id)
                    if possession_change.player_id
                    else None
                ),
                "kind": possession_change.kind,
            }
        history.append({
            "event_id": str(event.pk),
            "logical_event_id": str(event.logical_id),
            "sequence": event.sequence,
            "kind": event.kind,
            "status": (
                MatchEvent.STATUS_SUPERSEDED
                if event.sequence != latest_sequence_by_logical_id[event.logical_id]
                else (
                    MatchEvent.STATUS_RETRACTED
                    if event.kind.endswith(".retracted")
                    else MatchEvent.STATUS_ACTIVE
                )
            ),
            "source_type": event.source_type,
            "source_id": str(event.source_id),
            "match_part_id": str(event.period_id) if event.period_id else None,
            "effective_at": (
                event.effective_at.isoformat() if event.effective_at else None
            ),
            "elapsed_ms": event.elapsed_ms,
            "recorded_at": event.recorded_at.isoformat(),
            "command_id": str(event.command_id) if event.command_id else None,
            "actor_id": str(event.actor_id) if event.actor_id else None,
            "source": event.source,
            "device_id": event.device_id,
            "session_id": event.session_id,
            "payload_version": event.payload_version,
            "payload": event.payload,
            "supersedes_event_id": (
                str(event.supersedes_id) if event.supersedes_id else None
            ),
            "detail": detail,
            "observations": [
                {
                    "observation_id": str(observation.pk),
                    "command_id": (
                        str(observation.command_id) if observation.command_id else None
                    ),
                    "reporting_team_id": (
                        str(observation.reporting_team_id)
                        if observation.reporting_team_id
                        else None
                    ),
                    "actor_id": (
                        str(observation.actor_id) if observation.actor_id else None
                    ),
                    "source": observation.source,
                    "device_id": observation.device_id,
                    "session_id": observation.session_id,
                    "client_sequence": observation.client_sequence,
                    "effective_at": (
                        observation.effective_at.isoformat()
                        if observation.effective_at
                        else None
                    ),
                    "elapsed_ms": observation.elapsed_ms,
                    "origin": observation.origin,
                    "payload": observation.payload,
                    "recorded_at": observation.recorded_at.isoformat(),
                }
                for observation in event.observations.all()
            ],
        })
    return history
