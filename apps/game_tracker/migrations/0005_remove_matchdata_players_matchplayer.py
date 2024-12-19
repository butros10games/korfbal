# Generated by Django 5.1.1 on 2024-10-23 17:25

import django.db.models.deletion
import uuidv7.uuidv7
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("game_tracker", "0004_pause_active"),
        ("player", "0003_alter_player_profile_picture"),
        ("team", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="matchdata",
            name="players",
        ),
        migrations.CreateModel(
            name="MatchPlayer",
            fields=[
                (
                    "id_uuid",
                    models.UUIDField(
                        default=uuidv7.uuidv7.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "match_data",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="players",
                        to="game_tracker.matchdata",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="match_players",
                        to="player.player",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="match_players",
                        to="team.team",
                    ),
                ),
            ],
        ),
    ]
