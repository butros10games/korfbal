from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0015_playerpushsubscription"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="teams_visibility",
            field=models.CharField(
                choices=[("public", "Public"), ("club", "Club"), ("private", "Private")],
                default="public",
                max_length=16,
            ),
        ),
    ]
