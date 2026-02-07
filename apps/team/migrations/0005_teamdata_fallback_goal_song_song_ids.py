from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("team", "0004_teamdata_team_rank"),
    ]

    operations = [
        migrations.AddField(
            model_name="teamdata",
            name="fallback_goal_song_song_ids",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
