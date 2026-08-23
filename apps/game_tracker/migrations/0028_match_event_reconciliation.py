import bg_uuidv7.bg_uuidv7
from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models


def backfill_event_observations(apps, schema_editor):
    """Represent every historical envelope as one original observation."""
    del schema_editor
    MatchEvent = apps.get_model("game_tracker", "MatchEvent")
    MatchEventObservation = apps.get_model(
        "game_tracker", "MatchEventObservation"
    )
    observations = []
    for event in MatchEvent.objects.order_by("match_data_id", "sequence").iterator():
        observations.append(
            MatchEventObservation(
                match_data_id=event.match_data_id,
                event_id=event.pk,
                command_id=event.command_id,
                reporting_team_id=event.source_team_id,
                actor_id=event.actor_id,
                source=event.source,
                device_id=event.device_id,
                session_id=event.session_id,
                effective_at=event.effective_at,
                elapsed_ms=event.elapsed_ms,
                origin="canonical",
                payload=event.payload,
            )
        )
        if len(observations) >= 1_000:
            MatchEventObservation.objects.bulk_create(observations)
            observations.clear()
    if observations:
        MatchEventObservation.objects.bulk_create(observations)


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0027_tracker_command_reconciliation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MatchEventObservation",
            fields=[
                (
                    "id_uuid",
                    models.UUIDField(
                        default=bg_uuidv7.bg_uuidv7.uuidv7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("command_id", models.UUIDField(blank=True, null=True)),
                ("source", models.CharField(default="system", max_length=32)),
                ("device_id", models.CharField(blank=True, default="", max_length=128)),
                ("session_id", models.CharField(blank=True, default="", max_length=128)),
                ("client_sequence", models.PositiveBigIntegerField(blank=True, null=True)),
                ("effective_at", models.DateTimeField(blank=True, null=True)),
                ("elapsed_ms", models.PositiveBigIntegerField(blank=True, null=True)),
                (
                    "origin",
                    models.CharField(
                        choices=[
                            ("canonical", "Created canonical event"),
                            ("matched", "Matched existing event"),
                        ],
                        default="canonical",
                        max_length=12,
                    ),
                ),
                ("payload", models.JSONField(default=dict)),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="match_event_observations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="observations",
                        to="game_tracker.matchevent",
                    ),
                ),
                (
                    "match_data",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="event_observations",
                        to="game_tracker.matchdata",
                    ),
                ),
                (
                    "reporting_team",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="match_event_observations",
                        to="team.team",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="MatchEventReconciliation",
            fields=[
                (
                    "id_uuid",
                    models.UUIDField(
                        default=bg_uuidv7.bg_uuidv7.uuidv7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("confidence", models.PositiveSmallIntegerField()),
                ("reason", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "first_event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reconciliations_as_first",
                        to="game_tracker.matchevent",
                    ),
                ),
                (
                    "match_data",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="event_reconciliations",
                        to="game_tracker.matchdata",
                    ),
                ),
                (
                    "second_event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reconciliations_as_second",
                        to="game_tracker.matchevent",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="MatchEventReconciliationDecision",
            fields=[
                (
                    "id_uuid",
                    models.UUIDField(
                        default=bg_uuidv7.bg_uuidv7.uuidv7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "decision",
                    models.CharField(
                        choices=[("merge", "Merge"), ("separate", "Keep separate")],
                        max_length=8,
                    ),
                ),
                ("reason", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="match_event_reconciliation_decisions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "canonical_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reconciliation_decisions",
                        to="game_tracker.matchevent",
                    ),
                ),
                (
                    "reconciliation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="decision",
                        to="game_tracker.matcheventreconciliation",
                    ),
                ),
                (
                    "resolution_event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reconciliation_resolution",
                        to="game_tracker.matchevent",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="matcheventobservation",
            constraint=models.UniqueConstraint(
                condition=models.Q(command_id__isnull=False),
                fields=("event", "command_id"),
                name="game_tracker_unique_event_command_observation",
            ),
        ),
        migrations.AddIndex(
            model_name="matcheventobservation",
            index=models.Index(
                fields=["match_data", "recorded_at"],
                name="event_obs_match_time_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="matcheventobservation",
            index=models.Index(
                fields=["match_data", "reporting_team", "effective_at"],
                name="event_obs_team_time_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="matcheventreconciliation",
            constraint=models.UniqueConstraint(
                fields=("first_event", "second_event"),
                name="game_tracker_unique_reconciliation_pair",
            ),
        ),
        migrations.AddConstraint(
            model_name="matcheventreconciliation",
            constraint=models.CheckConstraint(
                condition=~models.Q(first_event=models.F("second_event")),
                name="game_tracker_distinct_reconciliation_events",
            ),
        ),
        migrations.AddIndex(
            model_name="matcheventreconciliation",
            index=models.Index(
                fields=["match_data", "-created_at"],
                name="reconcile_match_time_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="matcheventreconciliationdecision",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(decision="merge", canonical_event__isnull=False)
                    | models.Q(decision="separate", canonical_event__isnull=True)
                ),
                name="game_tracker_valid_reconciliation_decision",
            ),
        ),
        migrations.RunPython(
            backfill_event_observations,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
