"""Enforce a one-to-one relationship between Player and User."""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Count


def ensure_unique_player_per_user(apps, schema_editor) -> None:
    """Abort migration if duplicate Player rows exist for a user."""
    Player = apps.get_model("player", "Player")
    duplicates = Player.objects.values("user").annotate(count=Count("id_uuid")).filter(
        count__gt=1,
    )

    duplicate_user_ids = list(
        duplicates.values_list("user", flat=True).order_by(),
    )
    if duplicate_user_ids:
        raise RuntimeError(
            "Multiple Player records found for user ids: "
            f"{', '.join(str(user_id) for user_id in duplicate_user_ids)}. "
            "Please consolidate these records before re-running the migration.",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("player", "0005_player_goal_song_uri_player_song_start_time_and_more"),
    ]

    operations = [
        migrations.RunPython(
            ensure_unique_player_per_user,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="player",
            name="user",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="player",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
