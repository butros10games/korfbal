"""Record the first-run setup choice; existing accounts count as set up."""

from typing import Any

from django.db import migrations, models
from django.db.models.functions import Now


def mark_existing_accounts_set_up(apps: Any, _schema_editor: Any) -> None:
    """Only accounts created after the setup flow shipped are asked to set up."""
    Player = apps.get_model("player", "Player")
    Player._base_manager.filter(user__isnull=False, onboarded_at__isnull=True).update(
        onboarded_at=Now()
    )


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0033_retire_private_visibility"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="account_role",
            field=models.CharField(
                blank=True,
                choices=[
                    ("player", "Speler"),
                    ("club", "Clubvertegenwoordiger"),
                    ("spectator", "Toeschouwer"),
                ],
                db_default="",
                default="",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="onboarded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_accounts_set_up, migrations.RunPython.noop),
    ]
