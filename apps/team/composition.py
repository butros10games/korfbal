"""Bind team audio commands to the durable media job adapter."""

from functools import partial

from apps.player.composition import song_jobs
from apps.team.services.goal_songs import (
    create_team_song as _create_team_song,
    retry_team_song as _retry_team_song,
    update_team_song as _update_team_song,
)


create_team_song = partial(_create_team_song, jobs=song_jobs)
retry_team_song = partial(_retry_team_song, jobs=song_jobs)
update_team_song = partial(_update_team_song, jobs=song_jobs)
