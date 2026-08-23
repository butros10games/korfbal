"""Make pre-event-system matches fully replayable from canonical envelopes."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from django.db import migrations


SOURCE_MODELS = {
    "attack": "Attack",
    "match_part": "MatchPart",
    "pause": "Pause",
    "player_change": "PlayerChange",
    "shot": "Shot",
    "timeout": "Timeout",
}


def _json_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal, Enum)):
        return str(value)
    return str(value)


def _snapshot(instance):
    return {
        field.attname: _json_value(getattr(instance, field.attname))
        for field in instance._meta.concrete_fields
        if field.attname != "match_data_id"
    }


def _elapsed_ms(*, event, match_data, MatchPart, Pause):
    if event.period_id is None or event.effective_at is None:
        return None
    part = MatchPart.objects.filter(
        pk=event.period_id,
        match_data_id=match_data.pk,
    ).first()
    if part is None or part.start_time is None:
        return None

    pause_ms = 0
    intervals = Pause.objects.filter(
        match_data_id=match_data.pk,
        match_part_id=part.pk,
        start_time__lt=event.effective_at,
    ).values_list("start_time", "end_time")
    for start, end in intervals:
        if start is None:
            continue
        overlap_end = min(end or event.effective_at, event.effective_at)
        if overlap_end > start:
            pause_ms += int((overlap_end - start).total_seconds() * 1_000)

    period_offset_ms = (part.part_number - 1) * match_data.part_length * 1_000
    wall_ms = int((event.effective_at - part.start_time).total_seconds() * 1_000)
    return max(0, period_offset_ms + wall_ms - pause_ms)


def backfill_historical_event_snapshots(apps, schema_editor) -> None:
    """Snapshot the current historical fact for each active logical root."""
    del schema_editor
    MatchData = apps.get_model("game_tracker", "MatchData")
    MatchEvent = apps.get_model("game_tracker", "MatchEvent")
    MatchEventObservation = apps.get_model(
        "game_tracker",
        "MatchEventObservation",
    )
    MatchPart = apps.get_model("game_tracker", "MatchPart")
    Pause = apps.get_model("game_tracker", "Pause")
    models_by_source = {
        source_type: apps.get_model("game_tracker", model_name)
        for source_type, model_name in SOURCE_MODELS.items()
    }

    seen_roots = set()
    missing = []
    events = MatchEvent.objects.order_by(
        "match_data_id",
        "logical_id",
        "-sequence",
    )
    match_data_by_id = {
        match_data.pk: match_data for match_data in MatchData.objects.all().iterator()
    }
    for event in events.iterator(chunk_size=1_000):
        root = (event.match_data_id, event.logical_id)
        if root in seen_roots:
            continue
        seen_roots.add(root)
        if event.kind.endswith(".retracted"):
            continue
        model = models_by_source.get(event.source_type)
        if model is None:
            continue

        payload = dict(event.payload or {})
        record = payload.get("record")
        updates = {}
        if not isinstance(record, dict) or not record:
            projection = model.objects.filter(
                pk=event.source_id,
                match_data_id=event.match_data_id,
            ).first()
            if projection is None:
                missing.append((event.match_data_id, event.sequence, event.source_type))
                continue
            payload.update({
                "operation": payload.get("operation", "created"),
                "backfilled": True,
                "record": _snapshot(projection),
            })
            updates["payload"] = payload

        if event.elapsed_ms is None:
            match_data = match_data_by_id[event.match_data_id]
            elapsed_ms = _elapsed_ms(
                event=event,
                match_data=match_data,
                MatchPart=MatchPart,
                Pause=Pause,
            )
            if elapsed_ms is not None:
                updates["elapsed_ms"] = elapsed_ms
        if updates:
            MatchEvent.objects.filter(pk=event.pk).update(**updates)
            observation_updates = {
                field: value
                for field, value in updates.items()
                if field in {"payload", "elapsed_ms"}
            }
            if observation_updates:
                MatchEventObservation.objects.filter(
                    event_id=event.pk,
                    origin="canonical",
                ).update(**observation_updates)

    if missing:
        examples = ", ".join(
            f"{match_data_id}@{sequence}:{source_type}"
            for match_data_id, sequence, source_type in missing[:10]
        )
        msg = (
            f"Cannot make {len(missing)} active historical match events replayable; "
            f"missing projections: {examples}"
        )
        raise RuntimeError(msg)


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0029_match_event_projection_cutover"),
    ]

    operations = [
        migrations.RunPython(
            backfill_historical_event_snapshots,
            migrations.RunPython.noop,
        ),
    ]
