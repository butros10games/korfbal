"""Rebuild operational typed tables from the canonical match-event log."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import models, transaction
from django.utils.dateparse import parse_datetime

from apps.game_tracker.models import (
    Attack,
    MatchData,
    MatchEvent,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)
from apps.game_tracker.services.lineup_projections import rebuild_match_projections
from apps.game_tracker.services.match_event_context import (
    suppress_match_event_recording,
)
from apps.game_tracker.services.match_events import active_match_events


_PROJECTION_MODELS: dict[str, type[models.Model]] = {
    "match_part": MatchPart,
    "pause": Pause,
    "timeout": Timeout,
    "shot": Shot,
    "player_change": PlayerChange,
    "attack": Attack,
}
_DEPENDENT_SOURCE_TYPES = ("timeout", "shot", "player_change", "attack", "pause")


def _records_by_source(
    events: Iterable[MatchEvent],
) -> dict[str, list[tuple[MatchEvent, dict[str, Any]]]]:
    records: dict[str, list[tuple[MatchEvent, dict[str, Any]]]] = {
        source_type: [] for source_type in _PROJECTION_MODELS
    }
    for event in events:
        record = event.payload.get("record")
        if event.source_type in records and isinstance(record, dict):
            records[event.source_type].append((event, record))
    return records


def _projection_values(
    model: type[models.Model],
    event: MatchEvent,
    record: dict[str, Any],
    *,
    match_data_id: object,
) -> dict[str, Any]:
    """Deserialize one stored concrete-field snapshot for its typed table."""
    values: dict[str, Any] = {"match_data_id": match_data_id}
    for field in model._meta.concrete_fields:
        if field.primary_key or field.attname == "match_data_id":
            continue
        if field.attname not in record:
            continue
        value = record[field.attname]
        if isinstance(field, models.DateTimeField) and isinstance(value, str):
            parsed = parse_datetime(value)
            value = parsed if parsed is not None else value
        values[field.attname] = value
    values[model._meta.pk.attname] = event.source_id
    return values


def _rebuild_match_parts(
    rows: list[tuple[MatchEvent, dict[str, Any]]],
    *,
    match_data_id: object,
) -> None:
    """Upsert periods without severing surviving event-to-period references."""
    desired_ids = {event.source_id for event, _record in rows}
    MatchPart.objects.filter(match_data_id=match_data_id).update(active=False)
    MatchPart.objects.filter(match_data_id=match_data_id).exclude(
        pk__in=desired_ids
    ).delete()
    for event, record in rows:
        values = _projection_values(
            MatchPart,
            event,
            record,
            match_data_id=match_data_id,
        )
        primary_key = values.pop(MatchPart._meta.pk.attname)
        MatchPart.objects.update_or_create(pk=primary_key, defaults=values)


def rebuild_typed_event_projections(match_data: MatchData) -> None:
    """Restore typed query tables and aggregate state from immutable events.

    This operation is deterministic, transactional, and idempotent. It deliberately
    suppresses model-signal capture because replay materializes existing facts; it
    must never append new facts to the canonical log.
    """
    with transaction.atomic(), suppress_match_event_recording():
        locked = MatchData.objects.select_for_update().get(pk=match_data.pk)
        events = list(active_match_events(locked).order_by("sequence"))
        records = _records_by_source(events)

        # Remove dependants before periods so cascades cannot discard a row that
        # has already been reconstructed. Periods are upserted to preserve the
        # envelope's optional convenience FK for unchanged period identities.
        for source_type in _DEPENDENT_SOURCE_TYPES:
            model = _PROJECTION_MODELS[source_type]
            model.objects.filter(match_data_id=locked.pk).delete()

        _rebuild_match_parts(records["match_part"], match_data_id=locked.pk)

        for source_type in ("pause", "timeout", "shot", "player_change", "attack"):
            model = _PROJECTION_MODELS[source_type]
            model.objects.bulk_create(
                [
                    model(
                        **_projection_values(
                            model,
                            event,
                            record,
                            match_data_id=locked.pk,
                        )
                    )
                    for event, record in records[source_type]
                ]
            )

        rebuild_match_projections(locked)
