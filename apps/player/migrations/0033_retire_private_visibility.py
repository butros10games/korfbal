"""Store the retired 'private' visibility as the 'club' behaviour it already had."""

from typing import Any

from django.db import migrations, models


VISIBILITY_FIELDS = (
    "profile_picture_visibility",
    "stats_visibility",
    "teams_visibility",
)
VISIBILITY_CHOICES = [("public", "Public"), ("club", "Club")]


def private_to_club(apps: Any, _schema_editor: Any) -> None:
    """Rewrite every stored 'private' value, including archived players."""
    Player = apps.get_model("player", "Player")
    for field in VISIBILITY_FIELDS:
        Player._base_manager.filter(**{field: "private"}).update(**{field: "club"})


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0032_match_live_activity"),
    ]

    operations = [
        # Reversal keeps 'club': it is exactly how 'private' was enforced.
        migrations.RunPython(private_to_club, migrations.RunPython.noop),
        *[
            migrations.AlterField(
                model_name="player",
                name=field,
                field=models.CharField(
                    choices=VISIBILITY_CHOICES, default="public", max_length=16
                ),
            )
            for field in VISIBILITY_FIELDS
        ],
    ]
