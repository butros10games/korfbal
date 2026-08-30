"""Small typed builders for team API contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.club.models import Club
from apps.player.models import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


@dataclass(frozen=True, slots=True)
class TeamTestContext:
    """A team, current season, coach, and roster player."""

    club: Club
    team: Team
    season: Season
    team_data: TeamData
    coach: Player
    player: Player


def build_team_context(*, suffix: str = "contract") -> TeamTestContext:
    """Build the minimum persistent graph used by moderation tests."""
    today = timezone.localdate()
    season = Season.objects.create(
        name=f"Current {suffix}",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )
    club = Club.objects.create(name=f"Club {suffix}")
    team = Team.objects.create(name="Team 1", club=club)
    coach = create_player(username=f"coach_{suffix}")
    player = create_player(username=f"player_{suffix}")
    team_data = TeamData.objects.create(team=team, season=season)
    team_data.coach.add(coach)
    team_data.players.add(player)
    return TeamTestContext(
        club=club,
        team=team,
        season=season,
        team_data=team_data,
        coach=coach,
        player=player,
    )


def create_player(*, username: str) -> Player:
    """Create a user and return its signal-created player profile."""
    user = User.objects.create_user(
        username=username,
        password="pass1234",  # nosec
    )
    return Player.objects.get(user=user)


def create_song(
    *,
    player: Player,
    title: str,
    status: str = PlayerSongStatus.READY,
) -> PlayerSong:
    """Create a player song, including audio only for ready songs."""
    audio_file = (
        SimpleUploadedFile(
            f"{title}.mp3",
            b"ID3\x00\x00\x00\x00",
            content_type="audio/mpeg",
        )
        if status == PlayerSongStatus.READY
        else None
    )
    return PlayerSong.objects.create(
        player=player,
        title=title,
        artists="Test Artist",
        status=status,
        audio_file=audio_file,
    )
