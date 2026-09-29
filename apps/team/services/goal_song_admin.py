"""Team goal-song moderation reads and fallback playlist changes.

Callers authorize the viewer for the team season first; these functions scope
every song to that season's main roster or the team's own library.
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.player.models import Player
from apps.player.models.player_song import PlayerSong
from apps.player.services.goal_song import validate_ready_goal_songs
from apps.player.services.player_song_queries import (
    player_song_queryset,
    player_songs_by_ids,
    player_songs_for_players,
)
from apps.schedule.models import Season
from apps.team.models.team import Team
from apps.team.queries.overview import (
    main_roster_ids,
    team_data_for_season,
    team_matches,
    team_players,
)
from apps.team.services.goal_song_reads import (
    fallback_goal_song_song_ids,
    song_entries_for_ids,
)


class MissingTeamSeasonError(LookupError):
    """The team has no season record to hold a fallback playlist."""


@dataclass(frozen=True, slots=True)
class RosterPlayerSongs:
    """One main-roster player with their own songs and ordered selection."""

    player: Player
    songs: list[PlayerSong]
    selected_ids: list[str]
    selected: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class GoalSongAdminSnapshot:
    """Everything a team moderator sees for one season."""

    players: list[RosterPlayerSongs]
    team_songs: list[PlayerSong]
    fallback_ids: list[str]
    fallback_songs: list[dict[str, object]]


def goal_song_admin_snapshot(
    team: Team, season: Season | None
) -> GoalSongAdminSnapshot:
    """Read main-roster songs, team-owned songs and the fallback playlist.

    Returns:
        The moderation snapshot for the team season.

    """
    players = list(
        team_players(team, season, team_matches(team, season)).filter(
            id_uuid__in=main_roster_ids(team=team, season=season)
        )
    )
    songs = list(player_songs_for_players(players))
    team_data = team_data_for_season(team=team, season=season)
    team_songs = list(
        player_song_queryset().filter(team_data=team_data, team_data__isnull=False)
    )
    songs_by_player: dict[str, list[PlayerSong]] = {}
    for song in songs:
        songs_by_player.setdefault(str(song.player_id), []).append(song)
    fallback_ids = fallback_goal_song_song_ids(team=team, season=season)
    rows = []
    for player in players:
        own = songs_by_player.get(str(player.id_uuid), [])
        selected_ids = [
            song_id for song_id in player.goal_song_song_ids or [] if song_id
        ]
        rows.append(
            RosterPlayerSongs(
                player=player,
                songs=own,
                selected_ids=selected_ids,
                selected=song_entries_for_ids(songs=own, ids=selected_ids),
            )
        )
    return GoalSongAdminSnapshot(
        players=rows,
        team_songs=team_songs,
        fallback_ids=fallback_ids,
        fallback_songs=song_entries_for_ids(
            songs=[*songs, *team_songs], ids=fallback_ids
        ),
    )


def set_fallback_goal_songs(
    team: Team, season: Season | None, ids: list[str]
) -> list[dict[str, object]]:
    """Replace the season's fallback playlist after validating every entry.

    Team-owned songs may still be processing (only ready clips enter the match
    manifest); personal songs must be ready and owned by the main roster.

    Returns:
        The playlist entries for the saved selection.

    Raises:
        MissingTeamSeasonError: The team has no record for this season.

    """
    team_data = team_data_for_season(team=team, season=season)
    if team_data is None:
        raise MissingTeamSeasonError
    team_songs = list(player_songs_by_ids(song_ids=ids).filter(team_data=team_data))
    team_song_ids = {str(song.pk) for song in team_songs}
    personal_ids = [song_id for song_id in ids if song_id not in team_song_ids]
    personal_songs = (
        validate_ready_goal_songs(
            ids=personal_ids,
            songs=player_songs_by_ids(song_ids=personal_ids).filter(
                player_id__in=main_roster_ids(team=team, season=season)
            ),
        )
        if personal_ids
        else []
    )
    team_data.fallback_goal_song_song_ids = ids
    team_data.save(update_fields=["fallback_goal_song_song_ids"])
    return song_entries_for_ids(songs=[*personal_songs, *team_songs], ids=ids)
