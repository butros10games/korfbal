"""Join independently shipped access-link and tracker-integrity migrations."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0038_tracker_access_link"),
        (
            "game_tracker",
            "0038_remove_playermatchimpact_uniq_player_match_impact_and_more",
        ),
    ]

    operations = []
