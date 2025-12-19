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
from apps.game_tracker.models import GroupType, MatchData, PlayerGroup
from apps.schedule.models import Match, Season
from apps.team.models import Team


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
