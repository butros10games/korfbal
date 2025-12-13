"""Tests for game_tracker views."""

from datetime import timedelta
import json

from django.contrib.auth import get_user_model
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    GoalType,
    GroupType,
    MatchData,
    MatchPart,
    Pause,
    PlayerGroup,
    Shot,
)
from apps.schedule.models import Match, Season
from apps.team.models import Team


TEST_PASSWORD = "testpass123"  # noqa: S105  # nosec B105 - test credential constant
HTTP_STATUS_OK = 200
EXPECTED_PLAYER_GROUPS_COUNT = 6
EXPECTED_GROUPS_PER_TEAM = 3
EXPECTED_HOME_SCORE = 2
EXPECTED_AWAY_SCORE = 1
HTTP_STATUS_BAD_REQUEST = 400


@pytest.mark.django_db
def test_match_tracker_view_renders_correctly(client: Client) -> None:
    """Test that match tracker view renders with correct context."""
    # Create test data
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

    # Create goal types and group types needed for PlayerGroup creation
    GoalType.objects.create(name="Regular Goal")
    GroupType.objects.create(name="Aanval")
    GroupType.objects.create(name="Verdediging")
    GroupType.objects.create(name="Reserve")

    # Create authenticated user
    user = get_user_model().objects.create_user(
        username="testuser",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = client.get(
        reverse(
            "match_tracker",
            kwargs={"match_id": match.id_uuid, "team_id": home_team.id_uuid},
        ),
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK


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
        reverse("player_designation"),
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
        reverse("player_designation"),
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
        reverse("player_designation"),
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
def test_match_tracker_calculates_scores_correctly(client: Client) -> None:
    """Test that match tracker correctly calculates team scores."""
    # Create test data
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

    # Create players
    home_user = get_user_model().objects.create_user(
        username="home_player", password=TEST_PASSWORD
    )
    home_player = home_user.player
    away_user = get_user_model().objects.create_user(
        username="away_player", password=TEST_PASSWORD
    )
    away_player = away_user.player

    # Create shots
    Shot.objects.create(
        match_data=match_data, team=home_team, player=home_player, scored=True
    )
    Shot.objects.create(
        match_data=match_data, team=home_team, player=home_player, scored=True
    )
    Shot.objects.create(
        match_data=match_data, team=home_team, player=home_player, scored=False
    )  # Miss
    Shot.objects.create(
        match_data=match_data, team=away_team, player=away_player, scored=True
    )

    # Create goal types and group types
    GoalType.objects.create(name="Regular Goal")
    GroupType.objects.create(name="Aanval")
    GroupType.objects.create(name="Verdediging")
    GroupType.objects.create(name="Reserve")

    # Create authenticated user
    user = get_user_model().objects.create_user(
        username="testuser",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = client.get(
        reverse(
            "match_tracker",
            kwargs={"match_id": match.id_uuid, "team_id": home_team.id_uuid},
        ),
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    assert (
        response.context["team_1_score"] == EXPECTED_HOME_SCORE
    )  # 2 scored shots for home team
    assert (
        response.context["team_2_score"] == EXPECTED_AWAY_SCORE
    )  # 1 scored shot for away team


@pytest.mark.django_db
def test_match_tracker_creates_player_groups_automatically(client: Client) -> None:
    """Test that match tracker creates PlayerGroups automatically for both teams."""
    # Create test data
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

    # Create group types
    GroupType.objects.create(name="Aanval")
    GroupType.objects.create(name="Verdediging")
    GroupType.objects.create(name="Reserve")

    # Create authenticated user
    user = get_user_model().objects.create_user(
        username="testuser",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    # Initially no PlayerGroups should exist
    assert PlayerGroup.objects.count() == 0

    response = client.get(
        reverse(
            "match_tracker",
            kwargs={"match_id": match.id_uuid, "team_id": home_team.id_uuid},
        ),
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK

    # Should have created 6 PlayerGroups (3 groups x 2 teams)
    assert PlayerGroup.objects.count() == EXPECTED_PLAYER_GROUPS_COUNT

    # Check that groups were created for both teams
    home_groups = PlayerGroup.objects.filter(team=home_team, match_data=match_data)
    away_groups = PlayerGroup.objects.filter(team=away_team, match_data=match_data)

    assert home_groups.count() == EXPECTED_GROUPS_PER_TEAM
    assert away_groups.count() == EXPECTED_GROUPS_PER_TEAM

    # Check group names
    home_group_names = set(home_groups.values_list("starting_type__name", flat=True))
    away_group_names = set(away_groups.values_list("starting_type__name", flat=True))

    assert home_group_names == {"Aanval", "Verdediging", "Reserve"}
    assert away_group_names == {"Aanval", "Verdediging", "Reserve"}


@pytest.mark.django_db
def test_match_tracker_button_text_logic(client: Client) -> None:
    """Test that match tracker shows correct button text based on match state."""
    # Create test data
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

    # Test upcoming match
    match_data = MatchData.objects.get(match_link=match)

    GoalType.objects.create(name="Regular Goal")
    GroupType.objects.create(name="Aanval")
    GroupType.objects.create(name="Verdediging")
    GroupType.objects.create(name="Reserve")

    user = get_user_model().objects.create_user(
        username="testuser",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = client.get(
        reverse(
            "match_tracker",
            kwargs={"match_id": match.id_uuid, "team_id": home_team.id_uuid},
        ),
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    assert response.context["start_stop_button"] == "Start"

    # Test active match with no active parts or pauses
    match_data.status = "active"
    match_data.save()

    response = client.get(
        reverse(
            "match_tracker",
            kwargs={"match_id": match.id_uuid, "team_id": home_team.id_uuid},
        ),
        secure=True,
    )

    assert response.context["start_stop_button"] == "Start"

    # Test active match with active part (should show "Pause")
    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=timezone.now(),
        active=True,
    )

    response = client.get(
        reverse(
            "match_tracker",
            kwargs={"match_id": match.id_uuid, "team_id": home_team.id_uuid},
        ),
        secure=True,
    )

    assert response.context["start_stop_button"] == "Pause"

    # Test active match with active pause (should show "Start")
    Pause.objects.create(match_data=match_data, active=True)

    response = client.get(
        reverse(
            "match_tracker",
            kwargs={"match_id": match.id_uuid, "team_id": home_team.id_uuid},
        ),
        secure=True,
    )

    assert response.context["start_stop_button"] == "Start"
