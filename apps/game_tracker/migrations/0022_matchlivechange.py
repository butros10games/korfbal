from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0021_tracker_integrity_constraints"),
    ]

    operations = [
        migrations.CreateModel(
            name="MatchLiveChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("revision", models.PositiveBigIntegerField()),
                ("resources", models.JSONField(default=list)),
                ("changed_ids", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "match_data",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="live_changes",
                        to="game_tracker.matchdata",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["match_data", "revision"],
                        name="game_tracke_match_d_23208e_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("match_data", "revision"),
                        name="game_tracker_unique_live_revision",
                    ),
                ],
            },
        ),
    ]
