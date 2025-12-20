"""Tests for game_tracker endpoints.

The legacy Django-rendered match tracker views were removed when the project
migrated to a React SPA. These tests now target the REST endpoints that power
player selection and group designation.

"""

from datetime import timedelta
import json

from django.contrib.auth import get_user_model
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GroupType, MatchData, MatchPlayer, PlayerGroup
from apps.player.models import PlayerClubMembership
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


TEST_PASSWORD = "testpass123"  # noqa: S105  # nosec B105 - test credential constant
HTTP_STATUS_OK = 200
HTTP_STATUS_BAD_REQUEST = 400
EXPECTED_GROUPS_PER_TEAM = 3
EXPECTED_PLAYER_GROUPS_TOTAL = 6


@pytest.mark.django_db
def test_player_overview_data_creates_player_groups_automatically(
    client: Client,
) -> None:
    """Player selection API should create PlayerGroups automatically.

    Historically this happened in the server-rendered match tracker view.
    The SPA now relies on `/api/match/player_overview_data/<match>/<team>/`.
    """
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")

    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=30),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save()

    GroupType.objects.create(name="Aanval")
    GroupType.objects.create(name="Verdediging")
    GroupType.objects.create(name="Reserve")

    user = get_user_model().objects.create_user(
        username="testuser",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    assert PlayerGroup.objects.count() == 0

    response = client.get(
        f"/api/match/player_overview_data/{match.id_uuid}/{home_team.id_uuid}/",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert "player_groups" in payload

    # 3 groups (Aanval/Verdediging/Reserve) x 2 teams
    assert PlayerGroup.objects.count() == EXPECTED_PLAYER_GROUPS_TOTAL
    assert (
        PlayerGroup.objects.filter(team=home_team, match_data=match_data).count()
        == EXPECTED_GROUPS_PER_TEAM
    )
    assert (
        PlayerGroup.objects.filter(team=away_team, match_data=match_data).count()
        == EXPECTED_GROUPS_PER_TEAM
    )


@pytest.mark.django_db
def test_player_designation_allows_many_players_for_reserve_group(
    client: Client,
) -> None:
    """Reserve group should allow adding up to 16 players in one request."""
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=30),
    )
    match_data = MatchData.objects.get(match_link=match)

    reserve_type = GroupType.objects.create(name="Reserve")
    reserve_group = PlayerGroup.objects.create(
        match_data=match_data,
        team=home_team,
        starting_type=reserve_type,
        current_type=reserve_type,
    )

    user = get_user_model().objects.create_user(
        username="testuser",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    players = [
        get_user_model()
        .objects.create_user(
            username=f"p{i}",
            password=TEST_PASSWORD,
        )
        .player
        for i in range(8)
    ]

    response = client.post(
        "/api/match/player_designation/",
        data=json.dumps(
            {
                "new_group_id": str(reserve_group.id_uuid),
                "players": [{"id_uuid": str(p.id_uuid)} for p in players],
            },
        ),
        content_type="application/json",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    assert response.json() == {"success": True}
    assert reserve_group.players.count() == len(players)


@pytest.mark.django_db
def test_player_designation_rejects_more_than_16_total_in_reserve_group(
    client: Client,
) -> None:
    """Reserve group must never exceed 16 players total."""
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=30),
    )
    match_data = MatchData.objects.get(match_link=match)

    reserve_type = GroupType.objects.create(name="Reserve")
    reserve_group = PlayerGroup.objects.create(
        match_data=match_data,
        team=home_team,
        starting_type=reserve_type,
        current_type=reserve_type,
    )

    user = get_user_model().objects.create_user(
        username="testuser",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    initial_players = [
        get_user_model()
        .objects.create_user(
            username=f"p_initial_{i}",
            password=TEST_PASSWORD,
        )
        .player
        for i in range(10)
    ]
    reserve_group.players.add(*initial_players)
    reserve_group.refresh_from_db()
    assert reserve_group.players.count() == len(initial_players)

    new_players = [
        get_user_model()
        .objects.create_user(
            username=f"p_new_{i}",
            password=TEST_PASSWORD,
        )
        .player
        for i in range(7)
    ]

    response = client.post(
        "/api/match/player_designation/",
        data=json.dumps(
            {
                "new_group_id": str(reserve_group.id_uuid),
                "players": [{"id_uuid": str(p.id_uuid)} for p in new_players],
            },
        ),
        content_type="application/json",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_BAD_REQUEST
    assert response.json() == {"error": "Too many players selected"}
    reserve_group.refresh_from_db()
    assert reserve_group.players.count() == len(initial_players)


@pytest.mark.django_db
def test_player_designation_rejects_more_than_4_for_non_reserve_group(
    client: Client,
) -> None:
    """Non-reserve groups remain capped at 4 players."""
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=30),
    )
    match_data = MatchData.objects.get(match_link=match)

    attack_type = GroupType.objects.create(name="Aanval")
    attack_group = PlayerGroup.objects.create(
        match_data=match_data,
        team=home_team,
        starting_type=attack_type,
        current_type=attack_type,
    )

    user = get_user_model().objects.create_user(
        username="testuser",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    players = [
        get_user_model()
        .objects.create_user(
            username=f"p_attack_{i}",
            password=TEST_PASSWORD,
        )
        .player
        for i in range(5)
    ]

    response = client.post(
        "/api/match/player_designation/",
        data=json.dumps(
            {
                "new_group_id": str(attack_group.id_uuid),
                "players": [{"id_uuid": str(p.id_uuid)} for p in players],
            },
        ),
        content_type="application/json",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_BAD_REQUEST
    assert response.json() == {"error": "Too many players selected"}
    attack_group.refresh_from_db()
    assert attack_group.players.count() == 0


@pytest.mark.django_db
def test_player_designation_syncs_matchplayer_roster(
    client: Client,
) -> None:
    """Designating players into groups must create MatchPlayer rows.

    This is required so match stats can reliably place players on the correct
    side even when TeamData is missing or incomplete.
    """
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=5),
    )
    match_data = MatchData.objects.get(match_link=match)

    reserve_type = GroupType.objects.create(name="Reserve")
    reserve_group = PlayerGroup.objects.create(
        match_data=match_data,
        team=home_team,
        starting_type=reserve_type,
        current_type=reserve_type,
    )

    user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    players = [
        get_user_model()
        .objects.create_user(
            username=f"sync_p{i}",
            password=TEST_PASSWORD,
        )
        .player
        for i in range(2)
    ]

    response = client.post(
        "/api/match/player_designation/",
        data=json.dumps(
            {
                "new_group_id": str(reserve_group.id_uuid),
                "players": [{"id_uuid": str(p.id_uuid)} for p in players],
            },
        ),
        content_type="application/json",
        secure=True,
    )
    assert response.status_code == HTTP_STATUS_OK

    match_players_qs = MatchPlayer.objects.filter(match_data=match_data, team=home_team)
    assert match_players_qs.count() == len(players)
    assert {
        str(pid)
        for pid in match_players_qs.values_list("player_id", flat=True).distinct()
    } == {str(players[0].id_uuid), str(players[1].id_uuid)}

    # Removing a player from any group should remove them from the roster too.
    response_remove = client.post(
        "/api/match/player_designation/",
        data=json.dumps(
            {
                "new_group_id": None,
                "players": [
                    {
                        "id_uuid": str(players[0].id_uuid),
                        "groupId": str(reserve_group.id_uuid),
                    }
                ],
            },
        ),
        content_type="application/json",
        secure=True,
    )
    assert response_remove.status_code == HTTP_STATUS_OK
    assert (
        MatchPlayer.objects.filter(
            match_data=match_data,
            team=home_team,
            player_id=players[0].id_uuid,
        ).exists()
        is False
    )


@pytest.mark.django_db
def test_players_team_returns_empty_when_teamdata_missing(client: Client) -> None:
    """The available-players endpoint must not 500 when TeamData is absent."""
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    club = Club.objects.create(name="Club")
    opponent_club = Club.objects.create(name="Opponent")
    team = Team.objects.create(name="Team", club=club)
    opponent = Team.objects.create(name="Opponent Team", club=opponent_club)
    match = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now(),
    )

    response = client.get(
        f"/api/match/players_team/{match.id_uuid}/{team.id_uuid}/",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    assert response.json() == {"players": []}


@pytest.mark.django_db
def test_player_search_includes_club_membership_players(client: Client) -> None:
    """Search should include players that are club members (even without TeamData)."""
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    club = Club.objects.create(name="Search Club")
    opponent_club = Club.objects.create(name="Opponent")
    team = Team.objects.create(name="Team", club=club)
    opponent = Team.objects.create(name="Opponent Team", club=opponent_club)

    match = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now(),
    )

    member_user = get_user_model().objects.create_user(
        username="member_player",
        password=TEST_PASSWORD,
    )
    PlayerClubMembership.objects.create(player=member_user.player, club=club)

    response = client.get(
        f"/api/match/player_search/{match.id_uuid}/{team.id_uuid}/?search=member",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert payload["players"]
    assert {p["user"]["username"] for p in payload["players"]} == {"member_player"}


@pytest.mark.django_db
def test_player_search_includes_other_team_players_same_club(client: Client) -> None:
    """Search should include players from other teams within the same club.

    This is important for match rosters, where substitutes may come from
    different teams but still belong to the same club.
    """
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    club = Club.objects.create(name="Search Club")
    opponent_club = Club.objects.create(name="Opponent")

    team_a = Team.objects.create(name="Team A", club=club)
    team_b = Team.objects.create(name="Team B", club=club)
    opponent = Team.objects.create(name="Opponent Team", club=opponent_club)

    TeamData.objects.create(team=team_b, season=season)

    match = Match.objects.create(
        home_team=team_a,
        away_team=opponent,
        season=season,
        start_time=timezone.now(),
    )

    other_user = get_user_model().objects.create_user(
        username="clubmate_player",
        password=TEST_PASSWORD,
    )

    team_b_data = TeamData.objects.get(team=team_b, season=season)
    team_b_data.players.add(other_user.player)

    response = client.get(
        f"/api/match/player_search/{match.id_uuid}/{team_a.id_uuid}/?search=clubmate",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert payload["players"]
    assert {p["user"]["username"] for p in payload["players"]} == {"clubmate_player"}


@pytest.mark.django_db
def test_player_search_does_not_exclude_everything_when_groups_empty(
    client: Client,
) -> None:
    """Regression: empty PlayerGroups must not make search return zero players.

    When player groups exist but have no players, the exclusion subquery can
    accidentally contain NULLs and cause `NOT IN (NULL)` to filter out all rows.
    """
    season = Season.objects.create(
        name="2025 Season",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    club = Club.objects.create(name="Search Club")
    opponent_club = Club.objects.create(name="Opponent")
    team = Team.objects.create(name="Team", club=club)
    opponent = Team.objects.create(name="Opponent Team", club=opponent_club)

    match = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now(),
    )

    # Ensure player groups exist (authenticated flow).
    GroupType.objects.create(name="Aanval")
    GroupType.objects.create(name="Verdediging")
    GroupType.objects.create(name="Reserve")

    viewer = get_user_model().objects.create_user(
        username="viewer",
        password=TEST_PASSWORD,
    )
    client.force_login(viewer)

    resp_groups = client.get(
        f"/api/match/player_overview_data/{match.id_uuid}/{team.id_uuid}/",
        secure=True,
    )
    assert resp_groups.status_code == HTTP_STATUS_OK

    member_user = get_user_model().objects.create_user(
        username="daan_candidate",
        password=TEST_PASSWORD,
    )
    PlayerClubMembership.objects.create(player=member_user.player, club=club)

    response = client.get(
        f"/api/match/player_search/{match.id_uuid}/{team.id_uuid}/?search=daan",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert {p["user"]["username"] for p in payload["players"]} == {"daan_candidate"}
