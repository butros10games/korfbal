"""Add indexes for MatchPlayer.

These indexes speed up common lookups:
- Team/season roster derivation (team + match_data)
- Per-match roster checks (match_data + player)

"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0017_player_match_minutes"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="matchplayer",
            index=models.Index(fields=["team", "match_data"], name="mp_team_match_idx"),
        ),
        migrations.AddIndex(
            model_name="matchplayer",
            index=models.Index(
                fields=["match_data", "player"],
                name="mp_match_player_idx",
            ),
        ),
    ]
