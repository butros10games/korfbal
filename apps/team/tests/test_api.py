"""Tests for the team API endpoints."""

from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from unittest.mock import Mock
import uuid

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    MatchPlayer,
    PlayerMatchImpact,
    Shot,
)
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    persist_match_impact_rows_with_breakdowns,
)
from apps.player.models import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.schedule.models import Match, Season
from apps.team.models import Team
from apps.team.models.team_data import TeamData


pytestmark = pytest.mark.django_db


def _season(
    name: str = "2025",
    *,
    starts_in_days: int = -30,
    ends_in_days: int = 300,
) -> Season:
    today = timezone.localdate()
    return Season.objects.create(
        name=name,
        start_date=today + timedelta(days=starts_in_days),
        end_date=today + timedelta(days=ends_in_days),
    )


def _teams() -> tuple[Team, Team]:
    team = Team.objects.create(
        name="Team 1", club=Club.objects.create(name="Team Club")
    )
    opponent = Team.objects.create(
        name="Opponent 1", club=Club.objects.create(name="Opponent Club")
    )
    return team, opponent


def _player(username: str) -> Player:
    user = User.objects.create(username=username)
    return Player.objects.select_related("user").get(user=user)


def _roster(
    team: Team,
    season: Season,
    *players: Player,
    coach: Player | None = None,
) -> TeamData:
    team_data = TeamData.objects.create(team=team, season=season)
    team_data.players.add(*players)
    if coach is not None:
        team_data.coach.add(coach)
    return team_data


def _match(
    home_team: Team,
    away_team: Team,
    season: Season,
    *,
    starts_in_days: int,
    status: str,
) -> MatchData:
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() + timedelta(days=starts_in_days),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = status
    match_data.save(update_fields=["status"])
    return match_data


def _ready_song(
    player: Player,
    title: str,
    *,
    start_time_seconds: int = 0,
) -> PlayerSong:
    return PlayerSong.objects.create(
        player=player,
        title=title,
        artists=f"{title} Artist",
        status=PlayerSongStatus.READY,
        start_time_seconds=start_time_seconds,
        audio_file=SimpleUploadedFile(
            f"{title.lower().replace(' ', '-')}.mp3",
            b"ID3\x00\x00\x00\x00",
            content_type="audio/mpeg",
        ),
    )


@dataclass(frozen=True)
class _GoalSongSetup:
    team: Team
    team_data: TeamData
    coach: Player
    player: Player
    song_a: PlayerSong
    song_b: PlayerSong


def _coached_team() -> tuple[Team, TeamData, Player, Player]:
    season = _season()
    team, _ = _teams()
    coach = _player("coach")
    player = _player("team_player")
    team_data = _roster(team, season, coach, player, coach=coach)
    return team, team_data, coach, player


def _goal_song_setup() -> _GoalSongSetup:
    team, team_data, coach, player = _coached_team()
    return _GoalSongSetup(
        team=team,
        team_data=team_data,
        coach=coach,
        player=player,
        song_a=_ready_song(player, "Song A", start_time_seconds=3),
        song_b=_ready_song(player, "Song B", start_time_seconds=5),
    )


def _impact_setup(username: str) -> tuple[Season, Team, Player, MatchData]:
    season = _season(f"2025 - {username}")
    team, opponent = _teams()
    player = _player(username)
    match_data = _match(team, opponent, season, starts_in_days=-1, status="finished")
    return season, team, player, match_data


def test_team_overview_returns_current_matches_stats_and_roster(client: Client) -> None:
    """Current overview includes both match buckets, stats, and main roster."""
    season = _season()
    team, opponent = _teams()
    player = _player("player")
    _roster(team, season, player)
    _match(team, opponent, season, starts_in_days=3, status="upcoming")
    recent = _match(
        opponent,
        team,
        season,
        starts_in_days=-5,
        status="finished",
    )
    MatchData.objects.filter(pk=recent.pk).update(home_score=21, away_score=18)

    response = client.get(f"/api/team/teams/{team.id_uuid}/overview/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["team"]["id_uuid"] == str(team.id_uuid)
    assert payload["matches"]["upcoming"]
    assert payload["matches"]["recent"]
    assert payload["stats"]["general"] is not None
    assert payload["roster"][0]["username"] == player.user.username
    assert payload["roster"][0]["roster_role"] == "main"
    assert payload["meta"]["season_id"] == str(season.id_uuid)
    assert payload["meta"]["season_name"] == season.name
    assert any(option["is_current"] for option in payload["seasons"])


def test_team_overview_discovers_guest_and_shot_only_players(client: Client) -> None:
    """Events discover reserve players absent from the season roster."""
    season = _season()
    team, opponent = _teams()
    main = _player("main")
    guest = _player("guest")
    shot_only = _player("shot_only")
    _roster(team, season, main)
    match_data = _match(team, opponent, season, starts_in_days=-1, status="finished")
    MatchPlayer.objects.create(match_data=match_data, player=guest, team=team)
    Shot.objects.create(
        match_data=match_data, player=guest, team=team, for_team=True, scored=True
    )
    Shot.objects.create(
        match_data=match_data,
        player=shot_only,
        team=team,
        for_team=False,
        scored=False,
    )

    response = client.get(f"/api/team/teams/{team.id_uuid}/overview/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert [line["username"] for line in payload["stats"]["players"]] == [
        "guest",
        "shot_only",
    ]
    assert [(line["username"], line["roster_role"]) for line in payload["roster"]] == [
        ("main", "main"),
        ("guest", "reserve"),
        ("shot_only", "reserve"),
    ]


def test_team_overview_selects_historical_season(client: Client) -> None:
    """An explicit historical season scopes matches and roster."""
    current = _season()
    previous = _season("2024", starts_in_days=-400, ends_in_days=-35)
    team, opponent = _teams()
    player = _player("player")
    _roster(team, current, player)
    _roster(team, previous, player)
    historical = _match(
        team,
        opponent,
        previous,
        starts_in_days=-200,
        status="finished",
    )
    MatchData.objects.filter(pk=historical.pk).update(home_score=18, away_score=16)

    response = client.get(
        f"/api/team/teams/{team.id_uuid}/overview/",
        data={"season": previous.id_uuid},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["meta"]["season_id"] == str(previous.id_uuid)
    assert payload["matches"]["upcoming"] == []
    assert payload["matches"]["recent"][0]["competition"] == previous.name
    assert payload["roster"][0]["username"] == player.user.username
    assert len(payload["seasons"]) == 2


def test_team_overview_can_skip_stats_and_roster(client: Client) -> None:
    """Lightweight flags omit expensive stats and roster data."""
    season = _season()
    team, opponent = _teams()
    _roster(team, season, _player("player"))
    _match(team, opponent, season, starts_in_days=3, status="upcoming")

    response = client.get(
        f"/api/team/teams/{team.id_uuid}/overview/",
        data={"include_stats": "0", "include_roster": "0"},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["matches"]["upcoming"]
    assert payload["stats"] == {"general": None, "players": []}
    assert payload["roster"] == []


def test_team_overview_invalid_season_does_not_broaden(client: Client) -> None:
    """An invalid season falls back without mixing historical matches."""
    current = _season()
    previous = _season("2024", starts_in_days=-400, ends_in_days=-35)
    team, opponent = _teams()
    _roster(team, current)
    _roster(team, previous)
    _match(team, opponent, current, starts_in_days=2, status="upcoming")
    _match(
        opponent,
        team,
        previous,
        starts_in_days=-10,
        status="finished",
    )

    response = client.get(
        f"/api/team/teams/{team.id_uuid}/overview/",
        data={"season": str(uuid.uuid4())},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["meta"]["season_id"] == str(current.id_uuid)
    assert payload["meta"]["season_name"] == current.name
    assert payload["matches"]["upcoming"]
    assert payload["matches"]["recent"] == []


def test_team_overview_denies_goal_song_management_to_anonymous_viewer(
    client: Client,
) -> None:
    """Anonymous viewers cannot manage team goal songs."""
    team, _, _, _ = _coached_team()

    response = client.get(f"/api/team/teams/{team.id_uuid}/overview/")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["meta"]["viewer_can_manage_goal_songs"] is False


def test_team_overview_exposes_ordered_fallback_songs_to_coach(client: Client) -> None:
    """Coach metadata exposes fallback audio in configured order."""
    setup = _goal_song_setup()
    setup.team_data.fallback_goal_song_song_ids = [
        str(setup.song_b.id_uuid),
        str(setup.song_a.id_uuid),
    ]
    setup.team_data.save(update_fields=["fallback_goal_song_song_ids"])
    client.force_login(setup.coach.user)

    response = client.get(f"/api/team/teams/{setup.team.id_uuid}/overview/")

    assert response.status_code == HTTPStatus.OK
    meta = response.json()["meta"]
    assert meta["viewer_can_manage_goal_songs"] is True
    assert meta["fallback_goal_song_audio_urls"] == [
        setup.song_b.audio_file.url,
        setup.song_a.audio_file.url,
    ]


def test_team_impact_breakdown_returns_persisted_categories(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored impact breakdowns are returned without invoking self-heal."""
    season, team, player, match_data = _impact_setup("impact_player")
    part_start = timezone.now() - timedelta(minutes=10)
    part = MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=part_start,
        active=True,
    )
    Shot.objects.create(
        match_data=match_data,
        player=player,
        team=team,
        match_part=part,
        scored=False,
        time=part_start + timedelta(minutes=1),
    )
    persist_match_impact_rows_with_breakdowns(
        match_data=match_data,
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )

    def fail_self_heal(*args: object, **kwargs: object) -> None:
        raise AssertionError("persisted breakdown should not trigger self-heal")

    monkeypatch.setattr(
        "apps.team.api.views.persist_match_impact_rows_with_breakdowns",
        fail_self_heal,
    )
    response = client.get(
        f"/api/team/teams/{team.id_uuid}/impact-breakdown/",
        data={"season": season.id_uuid, "player": player.id_uuid},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["team_id"] == str(team.id_uuid)
    assert payload["season_id"] == str(season.id_uuid)
    assert payload["player_id"] == str(player.id_uuid)
    assert payload["algorithm_version"] == LATEST_MATCH_IMPACT_ALGORITHM_VERSION
    assert payload["matches_considered"] == 1
    assert any(
        category["key"] == "offense_miss_below_expected"
        for category in payload["categories"]
    )


def test_team_impact_breakdown_requires_player(client: Client) -> None:
    """The impact endpoint requires a player query parameter."""
    season = _season()
    team, _ = _teams()

    response = client.get(
        f"/api/team/teams/{team.id_uuid}/impact-breakdown/",
        data={"season": season.id_uuid},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Missing required query param: player"


def test_team_impact_breakdown_rejects_unknown_player(client: Client) -> None:
    """The impact endpoint rejects unknown player identifiers."""
    season = _season()
    team, _ = _teams()

    response = client.get(
        f"/api/team/teams/{team.id_uuid}/impact-breakdown/",
        data={"season": season.id_uuid, "player": str(uuid.uuid4())},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == "Player not found"


def test_team_impact_breakdown_tolerates_self_heal_failure(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed best-effort self-heal leaves stored impact totals usable."""
    season, team, player, match_data = _impact_setup("missing_breakdown")
    PlayerMatchImpact.objects.create(
        match_data=match_data,
        player=player,
        team=team,
        impact_score="3.2",
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )

    self_heal = Mock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "apps.team.api.views.persist_match_impact_rows_with_breakdowns", self_heal
    )
    response = client.get(
        f"/api/team/teams/{team.id_uuid}/impact-breakdown/",
        data={"season": season.id_uuid, "player": player.id_uuid},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["matches_considered"] == 1
    assert payload["impact_total"] == pytest.approx(3.2)
    assert payload["categories"] == []
    self_heal.assert_called_once_with(
        match_data=match_data,
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )


def test_team_goal_song_admin_requires_authentication(client: Client) -> None:
    """Goal-song administration rejects anonymous requests."""
    team, _, _, _ = _coached_team()

    response = client.get(f"/api/team/teams/{team.id_uuid}/goal-song-admin/")

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_team_goal_song_admin_returns_roster_to_coach(client: Client) -> None:
    """A coach can load the goal-song administration roster."""
    team, _, coach, _ = _coached_team()
    client.force_login(coach.user)

    response = client.get(f"/api/team/teams/{team.id_uuid}/goal-song-admin/")

    assert response.status_code == HTTPStatus.OK
    assert {entry["username"] for entry in response.json()["players"]} == {
        "coach",
        "team_player",
    }


def test_coach_updates_player_goal_song_selection(client: Client) -> None:
    """A coach can set an ordered ready-song selection for a player."""
    setup = _goal_song_setup()
    client.force_login(setup.coach.user)
    song_ids = [str(setup.song_a.id_uuid), str(setup.song_b.id_uuid)]

    response = client.patch(
        f"/api/team/teams/{setup.team.id_uuid}/goal-song-admin/player/{setup.player.id_uuid}/",
        data={"goal_song_song_ids": song_ids},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    setup.player.refresh_from_db()
    assert setup.player.goal_song_song_ids == song_ids
    assert setup.player.song_start_time == setup.song_a.start_time_seconds


def test_coach_updates_team_fallback_goal_songs(client: Client) -> None:
    """A coach can configure the team fallback playlist."""
    setup = _goal_song_setup()
    client.force_login(setup.coach.user)

    response = client.patch(
        f"/api/team/teams/{setup.team.id_uuid}/goal-song-admin/fallback/",
        data={"fallback_goal_song_song_ids": [str(setup.song_b.id_uuid)]},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    setup.team_data.refresh_from_db()
    assert setup.team_data.fallback_goal_song_song_ids == [str(setup.song_b.id_uuid)]


def test_coach_updates_player_song_settings(client: Client) -> None:
    """Song settings update both the song and selected player timing."""
    setup = _goal_song_setup()
    setup.player.goal_song_song_ids = [str(setup.song_a.id_uuid)]
    setup.player.save(update_fields=["goal_song_song_ids"])
    client.force_login(setup.coach.user)

    response = client.patch(
        (
            f"/api/team/teams/{setup.team.id_uuid}/goal-song-admin/player/"
            f"{setup.player.id_uuid}/songs/{setup.song_a.id_uuid}/settings/"
        ),
        data={"start_time_seconds": 12, "playback_speed": 1.1},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    setup.song_a.refresh_from_db()
    setup.player.refresh_from_db()
    assert setup.song_a.start_time_seconds == 12
    assert setup.song_a.playback_speed == pytest.approx(1.1)
    assert setup.player.song_start_time == 12


def test_coach_deletes_song_and_cleans_selections(client: Client) -> None:
    """Deleting a song removes it from player and fallback selections."""
    setup = _goal_song_setup()
    song_id = str(setup.song_b.id_uuid)
    setup.player.goal_song_song_ids = [str(setup.song_a.id_uuid), song_id]
    setup.player.save(update_fields=["goal_song_song_ids"])
    setup.team_data.fallback_goal_song_song_ids = [song_id]
    setup.team_data.save(update_fields=["fallback_goal_song_song_ids"])
    client.force_login(setup.coach.user)

    response = client.delete(
        f"/api/team/teams/{setup.team.id_uuid}/goal-song-admin/player/"
        f"{setup.player.id_uuid}/songs/{song_id}/"
    )

    assert response.status_code == HTTPStatus.NO_CONTENT
    setup.player.refresh_from_db()
    setup.team_data.refresh_from_db()
    assert song_id not in setup.player.goal_song_song_ids
    assert song_id not in setup.team_data.fallback_goal_song_song_ids
    assert not PlayerSong.objects.filter(id_uuid=song_id).exists()
