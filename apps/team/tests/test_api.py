"""Tests for the team API endpoints."""

from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from unittest.mock import Mock
import uuid

from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    MatchPlayer,
    PlayerMatchImpact,
    PossessionChange,
    Shot,
)
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    persist_match_impact_rows_with_breakdowns,
)
from apps.kwt_common.tests.api_test_support import assert_api_error
from apps.player.models import Player
from apps.player.models.player_song import PlayerSong
from apps.schedule.models import Match, Season
from apps.team.models import Team
from apps.team.models.team_data import TeamData

from .team_test_support import create_player, create_season, create_song


pytestmark = pytest.mark.django_db


def _teams() -> tuple[Team, Team]:
    team = Team.objects.create(
        name="Team 1", club=Club.objects.create(name="Team Club")
    )
    opponent = Team.objects.create(
        name="Opponent 1", club=Club.objects.create(name="Opponent Club")
    )
    return team, opponent


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


@dataclass(frozen=True)
class _GoalSongSetup:
    team: Team
    team_data: TeamData
    coach: Player
    player: Player
    song_a: PlayerSong
    song_b: PlayerSong


def _coached_team() -> tuple[Team, TeamData, Player, Player]:
    season = create_season()
    team, _ = _teams()
    coach = create_player(username="coach")
    player = create_player(username="team_player")
    team_data = _roster(team, season, coach, player, coach=coach)
    return team, team_data, coach, player


def _goal_song_setup() -> _GoalSongSetup:
    team, team_data, coach, player = _coached_team()
    return _GoalSongSetup(
        team=team,
        team_data=team_data,
        coach=coach,
        player=player,
        song_a=create_song(player=player, title="Song A", start_time_seconds=3),
        song_b=create_song(player=player, title="Song B", start_time_seconds=5),
    )


def _impact_setup(username: str) -> tuple[Season, Team, Player, MatchData]:
    season = create_season(f"2025 - {username}")
    team, opponent = _teams()
    player = create_player(username=username)
    match_data = _match(team, opponent, season, starts_in_days=-1, status="finished")
    return season, team, player, match_data


def test_team_overview_returns_current_matches_stats_and_roster(client: Client) -> None:
    """Current overview includes both match buckets, stats, and main roster."""
    season = create_season()
    team, opponent = _teams()
    player = create_player(username="player")
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
    assert payload["stats"]["general"] is None
    assert len(payload["stats"]["season"]["matches"]) == 1
    assert payload["roster"][0]["username"] == player.user.username
    assert payload["roster"][0]["roster_role"] == "main"
    assert payload["meta"]["season_id"] == str(season.id_uuid)
    assert payload["meta"]["season_name"] == season.name
    assert any(option["is_current"] for option in payload["seasons"])


@pytest.mark.parametrize("with_shots", [True, False])
def test_team_overview_counts_possession_events(
    client: Client, with_shots: bool
) -> None:
    """Season totals include possession-only players without multiplying events."""
    season = create_season()
    team, opponent = _teams()
    player = create_player(username="possession_player")
    _roster(team, season, player)
    for home, away in ((team, opponent), (opponent, team)):
        match_data = _match(home, away, season, starts_in_days=-1, status="finished")
        part = MatchPart.objects.create(
            match_data=match_data, part_number=1, start_time=timezone.now()
        )
        for kind in (
            PossessionChange.INTERCEPTION,
            PossessionChange.INTERCEPTION,
            PossessionChange.BALL_LOSS,
        ):
            PossessionChange.objects.create(
                match_data=match_data,
                match_part=part,
                team=team,
                player=player,
                kind=kind,
                time=timezone.now(),
            )
        PossessionChange.objects.create(
            match_data=match_data,
            match_part=part,
            team=opponent,
            kind=PossessionChange.BALL_LOSS,
            time=timezone.now(),
        )
        if with_shots:
            for _ in range(3):
                Shot.objects.create(
                    match_data=match_data, player=player, team=team, for_team=True
                )
    other_season = create_season("Historical season")
    excluded = _match(
        team, opponent, other_season, starts_in_days=-400, status="active"
    )
    part = MatchPart.objects.create(
        match_data=excluded, part_number=1, start_time=timezone.now()
    )
    PossessionChange.objects.create(
        match_data=excluded,
        match_part=part,
        team=team,
        player=player,
        kind=PossessionChange.BALL_LOSS,
        time=timezone.now(),
    )

    response = client.get(
        f"/api/team/teams/{team.id_uuid}/overview/", {"season": str(season.pk)}
    )

    assert response.status_code == HTTPStatus.OK
    line = next(
        row
        for row in response.json()["stats"]["players"]
        if row["id_uuid"] == str(player.pk)
    )
    assert line["interceptions"] == 4
    assert line["ball_losses"] == 2
    assert line["shots_for"] == (6 if with_shots else 0)


@pytest.mark.parametrize("is_home", [True, False])
def test_team_overview_discovers_guest_and_shot_only_players(
    client: Client,
    is_home: bool,
) -> None:
    """Events discover reserve players absent from the season roster."""
    season = create_season()
    team, opponent = _teams()
    main = create_player(username="main")
    guest = create_player(username="guest")
    shot_only = create_player(username="shot_only")
    _roster(team, season, main)
    home, away = (team, opponent) if is_home else (opponent, team)
    match_data = _match(home, away, season, starts_in_days=-1, status="finished")
    MatchPlayer.objects.create(match_data=match_data, player=guest, team=team)
    Shot.objects.create(
        match_data=match_data, player=guest, team=team, for_team=True, scored=True
    )
    Shot.objects.create(
        match_data=match_data,
        player=shot_only,
        team=opponent,
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

    opponent_attacker = create_player(username="opponent_attacker")
    opponent_defender = create_player(username="opponent_defender")
    Shot.objects.create(
        match_data=match_data,
        player=opponent_attacker,
        team=opponent,
        for_team=True,
        scored=True,
    )
    Shot.objects.create(
        match_data=match_data,
        player=opponent_defender,
        team=team,
        for_team=False,
        scored=True,
    )
    opponent_response = client.get(f"/api/team/teams/{opponent.id_uuid}/overview/")
    assert opponent_response.status_code == HTTPStatus.OK
    opponent_payload = opponent_response.json()
    assert [
        (line["username"], line["roster_role"]) for line in opponent_payload["roster"]
    ] == [
        ("opponent_attacker", "reserve"),
        ("opponent_defender", "reserve"),
    ]
    assert {line["username"] for line in opponent_payload["stats"]["players"]} == {
        "opponent_attacker",
        "opponent_defender",
    }
    team_response = client.get(f"/api/team/teams/{team.id_uuid}/overview/")
    assert team_response.status_code == HTTPStatus.OK
    assert {line["username"] for line in team_response.json()["roster"]} == {
        "main",
        "guest",
        "shot_only",
    }


def test_team_overview_selects_historical_season(client: Client) -> None:
    """An explicit historical season scopes matches and roster."""
    current = create_season()
    previous = create_season("2024", starts_in_days=-400, ends_in_days=-35)
    team, opponent = _teams()
    player = create_player(username="player")
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
    season = create_season()
    team, opponent = _teams()
    _roster(team, season, create_player(username="player"))
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
    current = create_season()
    previous = create_season("2024", starts_in_days=-400, ends_in_days=-35)
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
        "apps.team.services.impact_breakdowns.persist_match_impact_rows_with_breakdowns",
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
    season = create_season()
    team, _ = _teams()

    response = client.get(
        f"/api/team/teams/{team.id_uuid}/impact-breakdown/",
        data={"season": season.id_uuid},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Missing required query param: player"


def test_team_impact_breakdown_rejects_unknown_player(client: Client) -> None:
    """The impact endpoint rejects unknown player identifiers."""
    season = create_season()
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
        "apps.team.services.impact_breakdowns.persist_match_impact_rows_with_breakdowns",
        self_heal,
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


@pytest.mark.parametrize("viewer", ["anonymous", "player", "other-season-coach"])
@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("patch", "fallback/"),
        ("patch", "player/{player}/"),
        ("delete", "player/{player}/songs/{song}/"),
        ("patch", "player/{player}/songs/{song}/settings/"),
    ],
)
def test_team_goal_song_admin_requires_authentication(
    client: Client, viewer: str, method: str, suffix: str
) -> None:
    """Every moderation route requires an authorized viewer for the chosen season."""
    setup = _goal_song_setup()
    season = setup.team_data.season
    if viewer != "anonymous":
        actor = setup.coach if viewer == "other-season-coach" else setup.player
        client.force_login(actor.user)
    if viewer == "other-season-coach":
        season = create_season("Other season")
        _roster(setup.team, season, setup.player)
    path = suffix.format(player=setup.player.pk, song=setup.song_a.pk)
    response = getattr(client, method)(
        f"/api/team/teams/{setup.team.pk}/goal-song-admin/{path}?season={season.pk}",
        data={},
        content_type="application/json",
    )
    expected = (
        HTTPStatus.UNAUTHORIZED if viewer == "anonymous" else HTTPStatus.FORBIDDEN
    )
    assert response.status_code == expected
    if viewer != "anonymous":
        assert_api_error(
            response.json(),
            {"detail": "You do not have permission to manage team goal songs."},
        )


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


@pytest.mark.parametrize("search", ["team club 1", '"Team Club" 1', "CLUB,1"])
def test_team_catalog_search_preserves_cross_field_terms(
    client: Client, search: str
) -> None:
    """Each term can match either the club or team, including quoted phrases."""
    team, opponent = _teams()
    Team.objects.create(name="Team 2", club=team.club)
    Team.objects.create(name="Unrelated", club=opponent.club)
    response = client.get(
        "/api/team/teams/", {"search": search, "club": str(team.club_id)}
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["count"] == 1
    assert [row["id_uuid"] for row in response.json()["results"]] == [str(team.pk)]


def test_season_statistics_use_all_finished_results_and_actual_shooting_team(
    client: Client,
) -> None:
    """Pagination, recorder perspective and missing events cannot corrupt a season."""
    season = create_season()
    team, opponent = _teams()
    player = create_player(username="season_stats")
    _roster(team, season, player)
    completed = []
    for index in range(12):
        home, away = (team, opponent) if index % 2 == 0 else (opponent, team)
        data = _match(home, away, season, starts_in_days=index - 20, status="finished")
        MatchData.objects.filter(pk=data.pk).update(home_score=20, away_score=15)
        completed.append(data)
    # for_team deliberately disagrees with the selected team's perspective.
    for data in completed[:2]:
        Shot.objects.create(
            match_data=data, player=player, team=team, for_team=False, scored=True
        )
        Shot.objects.create(
            match_data=data, player=player, team=opponent, for_team=True, scored=False
        )
    Shot.objects.create(match_data=completed[2], player=player, team=None, scored=True)
    foreign_team = Team.objects.create(name="Other", club=team.club)
    Shot.objects.create(
        match_data=completed[2], player=player, team=foreign_team, scored=True
    )
    for status in ("active", "upcoming"):
        data = _match(team, opponent, season, starts_in_days=1, status=status)
        Shot.objects.create(match_data=data, player=player, team=team, scored=True)
    old_season = create_season("Old", starts_in_days=-600, ends_in_days=-300)
    data = _match(team, opponent, old_season, starts_in_days=-400, status="finished")
    Shot.objects.create(match_data=data, player=player, team=team, scored=True)

    # Shot-save signals keep tracker scores current; restore imported final scores
    # after constructing the deliberately partial shot registration.
    MatchData.objects.filter(pk__in=[item.pk for item in completed]).update(
        home_score=20, away_score=15, score_source="knkv"
    )
    response = client.get(
        f"/api/team/teams/{team.pk}/overview/", {"season": str(season.pk)}
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert len(payload["matches"]["recent"]) == 10
    results = payload["stats"]["season"]["matches"]
    assert len(results) == 12
    assert [row["match_id"] for row in results] == [
        str(data.match_link_id) for data in completed
    ]
    assert results[0]["goals_for"] == 20
    assert results[1]["goals_for"] == 15
    assert results[1]["goals_against"] == 20
    assert results[0]["is_home"] is True
    assert results[1]["is_home"] is False
    assert sum(row["has_shots"] for row in results) == 2
    general = payload["stats"]["general"]
    assert (general["shots_for"], general["shots_against"]) == (2, 2)
    assert (general["goals_for"], general["goals_against"]) == (2, 0)
    assert general["team_goal_stats"]["Onbekend"] == {
        "goals_by_player": 2,
        "goals_against_player": 0,
    }


def test_season_statistics_empty_and_live_only(client: Client) -> None:
    """No completed data is represented explicitly, including live player events."""
    season = create_season()
    team, opponent = _teams()
    player = create_player(username="live_only")
    _roster(team, season, player)
    data = _match(team, opponent, season, starts_in_days=0, status="active")
    Shot.objects.create(match_data=data, player=player, team=team, scored=True)
    response = client.get(
        f"/api/team/teams/{team.pk}/overview/", {"season": str(season.pk)}
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["stats"] == {
        "general": None,
        "season": {"matches": []},
        "players": [],
    }
