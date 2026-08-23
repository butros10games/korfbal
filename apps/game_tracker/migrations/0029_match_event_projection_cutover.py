"""Detach canonical facts from mutable projection lifecycle state."""

from __future__ import annotations

import uuid

from django.db import migrations, models


def backfill_period_ids(apps, schema_editor) -> None:
    """Retain period identity without depending on a MatchPart row."""
    del schema_editor
    MatchEvent = apps.get_model("game_tracker", "MatchEvent")
    for event in MatchEvent.objects.all().iterator(chunk_size=1000):
        period_id = event.match_part_id
        if period_id is None:
            record = event.payload.get("record", {})
            if isinstance(record, dict):
                raw_period_id = (
                    record.get("id_uuid")
                    if event.source_type == "match_part"
                    else record.get("match_part_id")
                )
                if raw_period_id:
                    try:
                        period_id = uuid.UUID(str(raw_period_id))
                    except (TypeError, ValueError, AttributeError):
                        period_id = None
        if period_id is not None:
            MatchEvent.objects.filter(pk=event.pk).update(period_id=period_id)


class Migration(migrations.Migration):
    dependencies = [("game_tracker", "0028_match_event_reconciliation")]

    operations = [
        migrations.AddField(
            model_name="matchevent",
            name="period_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_period_ids, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name="matchevent",
            name="match_event_source_idx",
        ),
        migrations.AddIndex(
            model_name="matchevent",
            index=models.Index(
                fields=["match_data", "source_type", "source_id"],
                name="match_event_source_v2_idx",
            ),
        ),
        migrations.RemoveField(model_name="matchevent", name="match_part"),
        migrations.RemoveField(model_name="matchevent", name="status"),
    ]
