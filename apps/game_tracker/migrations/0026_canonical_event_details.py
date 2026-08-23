import bg_uuidv7.bg_uuidv7
import django.db.models.deletion
from django.db import migrations, models


def backfill_canonical_event_fields(apps, schema_editor):
    """Connect existing envelopes to one logical fact and typed details."""
    del schema_editor
    MatchEvent = apps.get_model("game_tracker", "MatchEvent")
    Shot = apps.get_model("game_tracker", "Shot")
    ShotEventDetail = apps.get_model("game_tracker", "ShotEventDetail")
    PlayerChange = apps.get_model("game_tracker", "PlayerChange")
    PlayerGroup = apps.get_model("game_tracker", "PlayerGroup")
    SubstitutionEventDetail = apps.get_model(
        "game_tracker", "SubstitutionEventDetail"
    )

    logical_ids = {}
    for event in MatchEvent.objects.order_by("match_data_id", "sequence").iterator():
        root_key = (event.match_data_id, event.source_type, event.source_id)
        logical_id = logical_ids.setdefault(
            root_key,
            bg_uuidv7.bg_uuidv7.uuidv7(),
        )
        updates = {}
        if event.logical_id != logical_id:
            updates["logical_id"] = logical_id

        record = event.payload.get("record", {}) if event.payload else {}
        source = None
        if event.source_type == "match_part":
            updates["match_part_id"] = event.source_id
        elif event.source_type == "timeout":
            Timeout = apps.get_model("game_tracker", "Timeout")
            source = Timeout.objects.filter(pk=event.source_id).first()
            if source is not None:
                updates["match_part_id"] = source.match_part_id
        elif event.source_type in {
            "attack",
            "pause",
            "player_change",
            "shot",
        }:
            model = apps.get_model(
                "game_tracker",
                {
                    "attack": "Attack",
                    "pause": "Pause",
                    "player_change": "PlayerChange",
                    "shot": "Shot",
                }[event.source_type],
            )
            source = model.objects.filter(pk=event.source_id).first()
            if source is not None:
                updates["match_part_id"] = source.match_part_id

        if updates:
            MatchEvent.objects.filter(pk=event.pk).update(**updates)

        if event.source_type == "shot":
            shot = source or Shot.objects.filter(pk=event.source_id).first()
            player_id = record.get("player_id")
            team_id = record.get("team_id")
            shot_type_id = record.get("shot_type_id")
            scored = record.get("scored")
            for_team = record.get("for_team")
            if shot is not None:
                player_id = player_id or shot.player_id
                team_id = team_id or shot.team_id
                shot_type_id = shot_type_id or shot.shot_type_id
                if scored is None:
                    scored = shot.scored
                if for_team is None:
                    for_team = shot.for_team
            ShotEventDetail.objects.create(
                event_id=event.pk,
                shooting_team_id=team_id,
                shooter_id=player_id if for_team is not False else None,
                defender_id=player_id if for_team is False else None,
                shot_type_id=shot_type_id,
                outcome="goal" if scored else "miss",
            )
        elif event.source_type == "player_change":
            change = source or PlayerChange.objects.filter(
                pk=event.source_id
            ).first()
            group_id = record.get("player_group_id")
            player_in_id = record.get("player_in_id")
            player_out_id = record.get("player_out_id")
            if change is not None:
                group_id = group_id or change.player_group_id
                player_in_id = player_in_id or change.player_in_id
                player_out_id = player_out_id or change.player_out_id
            team_id = None
            if group_id is not None:
                team_id = PlayerGroup.objects.filter(pk=group_id).values_list(
                    "team_id", flat=True
                ).first()
            SubstitutionEventDetail.objects.create(
                event_id=event.pk,
                team_id=team_id,
                player_group_id=group_id,
                player_in_id=player_in_id,
                player_out_id=player_out_id,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0025_startingplayerassignment_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShotEventDetail",
            fields=[
                (
                    "event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="shot_detail",
                        serialize=False,
                        to="game_tracker.matchevent",
                    ),
                ),
                (
                    "outcome",
                    models.CharField(
                        choices=[("goal", "Goal"), ("miss", "Miss")],
                        max_length=8,
                    ),
                ),
                (
                    "defender",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="canonical_shots_defended",
                        to="player.player",
                    ),
                ),
                (
                    "shooter",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="canonical_shots_taken",
                        to="player.player",
                    ),
                ),
                (
                    "shooting_team",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="canonical_shot_events",
                        to="team.team",
                    ),
                ),
                (
                    "shot_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="canonical_shot_events",
                        to="game_tracker.goaltype",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SubstitutionEventDetail",
            fields=[
                (
                    "event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="substitution_detail",
                        serialize=False,
                        to="game_tracker.matchevent",
                    ),
                ),
                (
                    "player_group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="canonical_substitution_events",
                        to="game_tracker.playergroup",
                    ),
                ),
                (
                    "player_in",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="canonical_substitutions_in",
                        to="player.player",
                    ),
                ),
                (
                    "player_out",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="canonical_substitutions_out",
                        to="player.player",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="canonical_substitution_events",
                        to="team.team",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="matchevent",
            name="device_id",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="matchevent",
            name="logical_id",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="matchevent",
            name="match_part",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="domain_events",
                to="game_tracker.matchpart",
            ),
        ),
        migrations.AddField(
            model_name="matchevent",
            name="session_id",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="matchevent",
            name="source",
            field=models.CharField(default="system", max_length=32),
        ),
        migrations.RunPython(
            backfill_canonical_event_fields,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="matchevent",
            name="logical_id",
            field=models.UUIDField(
                default=bg_uuidv7.bg_uuidv7.uuidv7,
                editable=False,
            ),
        ),
        migrations.AddIndex(
            model_name="matchevent",
            index=models.Index(
                fields=["match_data", "logical_id", "-sequence"],
                name="match_event_logical_idx",
            ),
        ),
    ]
