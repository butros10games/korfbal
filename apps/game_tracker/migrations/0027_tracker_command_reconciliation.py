import bg_uuidv7.bg_uuidv7
from django.db import migrations, models
from django.db.models import Count


def reconcile_global_command_ids(apps, schema_editor):
    """Keep the earliest receipt ID and re-key cross-match duplicates."""
    del schema_editor
    TrackerCommand = apps.get_model("game_tracker", "TrackerCommand")
    duplicate_ids = (
        TrackerCommand.objects
        .values("command_id")
        .annotate(total=Count("id_uuid"))
        .filter(total__gt=1)
        .values_list("command_id", flat=True)
    )
    for command_id in duplicate_ids.iterator():
        duplicate_receipts = list(
            TrackerCommand.objects
            .filter(command_id=command_id)
            .order_by("created_at", "id_uuid")
            .values_list("id_uuid", flat=True)
        )
        for receipt_id in duplicate_receipts[1:]:
            replacement = bg_uuidv7.bg_uuidv7.uuidv7()
            while TrackerCommand.objects.filter(command_id=replacement).exists():
                replacement = bg_uuidv7.bg_uuidv7.uuidv7()
            TrackerCommand.objects.filter(pk=receipt_id).update(
                command_id=replacement
            )


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0026_canonical_event_details"),
    ]

    operations = [
        migrations.RunPython(
            reconcile_global_command_ids,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveConstraint(
            model_name="trackercommand",
            name="game_tracker_unique_command_id",
        ),
        migrations.AlterField(
            model_name="trackercommand",
            name="command_id",
            field=models.UUIDField(unique=True),
        ),
        migrations.AddField(
            model_name="trackercommand",
            name="client_sequence",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="trackercommand",
            name="committed_revision",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="trackercommand",
            name="device_id",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="trackercommand",
            name="response_payload",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="trackercommand",
            name="session_id",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="trackercommand",
            name="source",
            field=models.CharField(default="tracker", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="trackercommand",
            constraint=models.UniqueConstraint(
                condition=(
                    ~models.Q(device_id="")
                    & models.Q(client_sequence__isnull=False)
                ),
                fields=("match_data", "device_id", "client_sequence"),
                name="game_tracker_unique_device_command_sequence",
            ),
        ),
        migrations.AddIndex(
            model_name="trackercommand",
            index=models.Index(
                fields=["match_data", "device_id", "client_sequence"],
                name="tracker_cmd_device_seq_idx",
            ),
        ),
    ]
