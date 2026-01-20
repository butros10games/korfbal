from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0016_player_teams_visibility"),
    ]

    operations = [
        migrations.AddField(
            model_name="playerpushsubscription",
            name="platform",
            field=models.CharField(default="web", max_length=16),
        ),
    ]
