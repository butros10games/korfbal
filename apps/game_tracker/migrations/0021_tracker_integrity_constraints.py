"""Normalize tracker timer state and add database integrity constraints."""

from __future__ import annotations

from django.db import migrations, models
from django.db.models import Count, F, Q


def normalize_tracker_state(apps, schema_editor) -> None:
    """Make legacy rows safe for the new uniqueness/check constraints."""
    del schema_editor
    MatchData = apps.get_model("game_tracker", "MatchData")
    MatchPart = apps.get_model("game_tracker", "MatchPart")
    Pause = apps.get_model("game_tracker", "Pause")

    MatchPart.objects.filter(end_time__lt=F("start_time")).update(
        end_time=F("start_time")
    )
    Pause.objects.filter(end_time__isnull=False, start_time__isnull=True).update(
        end_time=None
    )
    Pause.objects.filter(end_time__lt=F("start_time")).update(end_time=F("start_time"))
    Pause.objects.filter(active=True, end_time__isnull=False).update(active=False)
    Pause.objects.filter(active=True, start_time__isnull=True).update(active=False)

    duplicate_match_ids = (
        MatchData.objects.values("match_link_id")
        .annotate(row_count=Count("id_uuid"))
        .filter(row_count__gt=1)
        .values_list("match_link_id", flat=True)
    )
    for match_id in list(duplicate_match_ids):
        rows = list(
            MatchData.objects.filter(match_link_id=match_id).order_by(
                models.Case(
                    models.When(status="finished", then=0),
                    models.When(status="active", then=1),
                    default=2,
                ),
                "-live_revision",
                "id_uuid",
            )
        )
        # Duplicate MatchData rows are invalid and their child timelines cannot
        # safely be merged (part numbers and active state may conflict). Keep the
        # most authoritative row and let CASCADE remove the invalid duplicates.
        MatchData.objects.filter(id_uuid__in=[row.id_uuid for row in rows[1:]]).delete()

    duplicate_part_keys = (
        MatchPart.objects.values("match_data_id", "part_number")
        .annotate(row_count=Count("id_uuid"))
        .filter(row_count__gt=1)
    )
    for key in list(duplicate_part_keys):
        parts = list(
            MatchPart.objects.filter(
                match_data_id=key["match_data_id"],
                part_number=key["part_number"],
            ).order_by("-active", "-start_time", "-id_uuid")
        )
        MatchPart.objects.filter(id_uuid__in=[part.id_uuid for part in parts[1:]]).delete()

    active_part_match_ids = (
        MatchPart.objects.filter(active=True)
        .values("match_data_id")
        .annotate(row_count=Count("id_uuid"))
        .filter(row_count__gt=1)
        .values_list("match_data_id", flat=True)
    )
    for match_data_id in list(active_part_match_ids):
        active_parts = list(
            MatchPart.objects.filter(
                match_data_id=match_data_id,
                active=True,
            ).order_by("-start_time", "-id_uuid")
        )
        for stale_part in active_parts[1:]:
            stale_part.active = False
            stale_part.end_time = max(stale_part.start_time, active_parts[0].start_time)
            stale_part.save(update_fields=["active", "end_time"])

    active_pause_match_ids = (
        Pause.objects.filter(active=True)
        .values("match_data_id")
        .annotate(row_count=Count("id_uuid"))
        .filter(row_count__gt=1)
        .values_list("match_data_id", flat=True)
    )
    for match_data_id in list(active_pause_match_ids):
        active_pauses = list(
            Pause.objects.filter(
                match_data_id=match_data_id,
                active=True,
            ).order_by("-start_time", "-id_uuid")
        )
        keeper = active_pauses[0]
        for stale_pause in active_pauses[1:]:
            stale_pause.active = False
            stale_pause.end_time = max(
                stale_pause.start_time or keeper.start_time,
                keeper.start_time,
            )
            stale_pause.save(update_fields=["active", "end_time"])


class Migration(migrations.Migration):
    dependencies = [("game_tracker", "0020_matchdata_live_revision")]

    operations = [
        migrations.RunPython(normalize_tracker_state, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="matchdata",
            constraint=models.UniqueConstraint(
                fields=("match_link",),
                name="uniq_match_data_per_match",
            ),
        ),
        migrations.AddConstraint(
            model_name="matchpart",
            constraint=models.UniqueConstraint(
                fields=("match_data", "part_number"),
                name="uniq_match_part_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="matchpart",
            constraint=models.UniqueConstraint(
                condition=Q(active=True),
                fields=("match_data",),
                name="uniq_active_match_part",
            ),
        ),
        migrations.AddConstraint(
            model_name="matchpart",
            constraint=models.CheckConstraint(
                condition=Q(end_time__isnull=True) | Q(end_time__gte=F("start_time")),
                name="match_part_end_after_start",
            ),
        ),
        migrations.AddConstraint(
            model_name="pause",
            constraint=models.UniqueConstraint(
                condition=Q(active=True),
                fields=("match_data",),
                name="uniq_active_match_pause",
            ),
        ),
        migrations.AddConstraint(
            model_name="pause",
            constraint=models.CheckConstraint(
                condition=Q(end_time__isnull=True)
                | (Q(start_time__isnull=False) & Q(end_time__gte=F("start_time"))),
                name="pause_end_after_start",
            ),
        ),
        migrations.AddConstraint(
            model_name="pause",
            constraint=models.CheckConstraint(
                condition=Q(active=False)
                | (Q(start_time__isnull=False) & Q(end_time__isnull=True)),
                name="active_pause_has_open_start",
            ),
        ),
    ]
