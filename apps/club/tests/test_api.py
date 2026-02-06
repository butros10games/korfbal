"""Tests for the club API endpoints."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from http import HTTPStatus
import uuid

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData
from apps.game_tracker.models.player_match_minutes import (
    LATEST_MATCH_MINUTES_VERSION,
    PlayerMatchMinutes,
)
from apps.player.models import PlayerClubMembership
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


EXPECTED_PLAYED_MATCHES = 3


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_overview_returns_team_and_match_payload(client: Client) -> None:
    """The overview endpoint should include teams plus upcoming and recent matches."""
    season = Season.objects.create(
        name="2025/2026",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    club = Club.objects.create(name="Test Club")
    opponent_club = Club.objects.create(name="Opponent Club")

    team = Team.objects.create(name="Test Team", club=club)
    opponent_team = Team.objects.create(name="Opponent Team", club=opponent_club)

    future_match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now() + timedelta(days=2),
    )
    past_match = Match.objects.create(
        home_team=opponent_team,
        away_team=team,
        season=season,
        start_time=timezone.now() - timedelta(days=4),
    )

    future_match_data = MatchData.objects.get(match_link=future_match)
    future_match_data.status = "upcoming"
    future_match_data.save(update_fields=["status"])

    past_match_data = MatchData.objects.get(match_link=past_match)
    past_match_data.status = "finished"
    past_match_data.home_score = 15
    past_match_data.away_score = 14
    past_match_data.save(update_fields=["status", "home_score", "away_score"])

    response = client.get(f"/api/club/clubs/{club.id_uuid}/overview/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert payload["club"]["id_uuid"] == str(club.id_uuid)
    assert len(payload["teams"]) == 1
    assert payload["teams"][0]["name"] == team.name
    assert payload["matches"]["upcoming"][0]["status"] == "upcoming"
    assert payload["matches"]["recent"][0]["status"] == "finished"
    assert payload["meta"]["season_id"] == str(season.id_uuid)
    assert payload["meta"]["season_name"] == season.name
    assert payload["seasons"]
    assert payload["seasons"][0]["id_uuid"] == str(season.id_uuid)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_overview_can_filter_by_season(client: Client) -> None:
    """Explicit season query should scope teams and matches."""
    today = timezone.now().date()
    current_season = Season.objects.create(
        name="2025/2026",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=335),
    )
    previous_season = Season.objects.create(
        name="2024/2025",
        start_date=today - timedelta(days=400),
        end_date=today - timedelta(days=35),
    )

    club = Club.objects.create(name="Filter Club")
    opponent_club = Club.objects.create(name="Filter Opponent")

    recent_team = Team.objects.create(name="Recent Team", club=club)
    legacy_team = Team.objects.create(name="Legacy Team", club=club)
    opponent_team = Team.objects.create(name="Opponent Team", club=opponent_club)

    TeamData.objects.create(team=recent_team, season=current_season)
    TeamData.objects.create(team=legacy_team, season=previous_season)

    recent_match = Match.objects.create(
        home_team=recent_team,
        away_team=opponent_team,
        season=current_season,
        start_time=timezone.now() + timedelta(days=3),
    )
    legacy_match = Match.objects.create(
        home_team=opponent_team,
        away_team=legacy_team,
        season=previous_season,
        start_time=timezone.now() - timedelta(days=10),
    )

    recent_data = MatchData.objects.get(match_link=recent_match)
    recent_data.status = "upcoming"
    recent_data.save(update_fields=["status"])

    legacy_data = MatchData.objects.get(match_link=legacy_match)
    legacy_data.status = "finished"
    legacy_data.home_score = 18
    legacy_data.away_score = 14
    legacy_data.save(update_fields=["status", "home_score", "away_score"])

    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/overview/",
        {"season": str(previous_season.id_uuid)},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert payload["meta"]["season_id"] == str(previous_season.id_uuid)
    assert payload["meta"]["season_name"] == previous_season.name
    assert [team["name"] for team in payload["teams"]] == ["Legacy Team"]
    assert payload["matches"]["upcoming"] == []
    assert payload["matches"]["recent"][0]["status"] == "finished"
    assert payload["matches"]["recent"][0]["competition"] == previous_season.name


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_settings_visible_only_for_club_admin(client: Client) -> None:
    """Club settings endpoints must be accessible only for club admins."""
    club = Club.objects.create(name="Admin Club")

    viewer = get_user_model().objects.create_user(
        username="viewer",
        password="pass1234",  # noqa: S106  # nosec
    )
    admin_user = get_user_model().objects.create_user(
        username="club_admin",
        password="pass1234",  # noqa: S106  # nosec
    )
    admin_player = admin_user.player
    club.admin.add(admin_player)

    client.force_login(viewer)
    response_forbidden = client.get(f"/api/club/clubs/{club.id_uuid}/settings/")
    assert response_forbidden.status_code == HTTPStatus.FORBIDDEN

    client.force_login(admin_user)
    response_ok = client.get(f"/api/club/clubs/{club.id_uuid}/settings/")
    assert response_ok.status_code == HTTPStatus.OK
    payload = response_ok.json()
    assert payload["club"]["id_uuid"] == str(club.id_uuid)
    assert payload["admins"][0]["username"] == "club_admin"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_admin_can_add_and_remove_memberships(client: Client) -> None:
    """Club admins can add users to a club and remove them (close membership)."""
    club = Club.objects.create(name="Membership Club")

    admin_user = get_user_model().objects.create_user(
        username="club_admin",
        password="pass1234",  # noqa: S106  # nosec
    )
    club.admin.add(admin_user.player)

    member_user = get_user_model().objects.create_user(
        username="member",
        password="pass1234",  # noqa: S106  # nosec
    )

    client.force_login(admin_user)

    response_add = client.post(
        f"/api/club/clubs/{club.id_uuid}/memberships/",
        data={"username": "member"},
        content_type="application/json",
    )
    assert response_add.status_code == HTTPStatus.CREATED

    membership = PlayerClubMembership.objects.get(
        club_id=club.id_uuid,
        player__user__username="member",
        end_date__isnull=True,
    )
    assert membership.start_date <= timezone.localdate()

    response_settings = client.get(f"/api/club/clubs/{club.id_uuid}/settings/")
    assert response_settings.status_code == HTTPStatus.OK
    settings_payload = response_settings.json()
    assert [m["player"]["username"] for m in settings_payload["members"]] == ["member"]

    response_remove = client.delete(
        f"/api/club/clubs/{club.id_uuid}/memberships/{member_user.player.id_uuid}/"
    )
    assert response_remove.status_code == HTTPStatus.NO_CONTENT

    membership.refresh_from_db()
    assert membership.end_date == timezone.localdate()


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_overview_invalid_season_does_not_broaden(client: Client) -> None:
    """An invalid season query should fall back to a club season, not broaden."""
    today = timezone.now().date()
    current_season = Season.objects.create(
        name="2025/2026",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=335),
    )
    previous_season = Season.objects.create(
        name="2024/2025",
        start_date=today - timedelta(days=400),
        end_date=today - timedelta(days=35),
    )

    club = Club.objects.create(name="Scope Club")
    opponent_club = Club.objects.create(name="Opponent Club")

    current_team = Team.objects.create(name="Current Team", club=club)
    previous_team = Team.objects.create(name="Previous Team", club=club)
    opponent_team = Team.objects.create(name="Opponent Team", club=opponent_club)

    TeamData.objects.create(team=current_team, season=current_season)
    TeamData.objects.create(team=previous_team, season=previous_season)

    current_match = Match.objects.create(
        home_team=current_team,
        away_team=opponent_team,
        season=current_season,
        start_time=timezone.now() + timedelta(days=2),
    )
    previous_match = Match.objects.create(
        home_team=opponent_team,
        away_team=previous_team,
        season=previous_season,
        start_time=timezone.now() - timedelta(days=10),
    )

    current_data = MatchData.objects.get(match_link=current_match)
    current_data.status = "upcoming"
    current_data.save(update_fields=["status"])

    previous_data = MatchData.objects.get(match_link=previous_match)
    previous_data.status = "finished"
    previous_data.save(update_fields=["status"])

    invalid_season_id = str(uuid.uuid4())
    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/overview/",
        {"season": invalid_season_id},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert payload["meta"]["season_id"] == str(current_season.id_uuid)
    assert payload["meta"]["season_name"] == current_season.name
    assert [team["name"] for team in payload["teams"]] == ["Current Team"]
    assert payload["matches"]["upcoming"]
    assert payload["matches"]["recent"] == []


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_overview_meta_viewer_is_admin(client: Client) -> None:
    """The overview endpoint should report whether the viewer is a club admin."""
    season = Season.objects.create(
        name="2025/2026",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=365),
    )
    club = Club.objects.create(name="Admin Meta Club")
    opponent_club = Club.objects.create(name="Opponent Club")
    team = Team.objects.create(name="Team", club=club)
    opponent_team = Team.objects.create(name="Opponent", club=opponent_club)

    TeamData.objects.create(team=team, season=season)
    match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now() + timedelta(days=2),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "upcoming"
    match_data.save(update_fields=["status"])

    response_anon = client.get(f"/api/club/clubs/{club.id_uuid}/overview/")
    assert response_anon.status_code == HTTPStatus.OK
    assert response_anon.json()["meta"]["viewer_is_admin"] is False

    viewer = get_user_model().objects.create_user(
        username="viewer_meta",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(viewer)
    response_user = client.get(f"/api/club/clubs/{club.id_uuid}/overview/")
    assert response_user.status_code == HTTPStatus.OK
    assert response_user.json()["meta"]["viewer_is_admin"] is False

    admin_user = get_user_model().objects.create_user(
        username="admin_meta",
        password="pass1234",  # noqa: S106  # nosec
    )
    club.admin.add(admin_user.player)
    client.force_login(admin_user)
    response_admin = client.get(f"/api/club/clubs/{club.id_uuid}/overview/")
    assert response_admin.status_code == HTTPStatus.OK
    assert response_admin.json()["meta"]["viewer_is_admin"] is True


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_settings_user_search_requires_admin_and_min_length(
    client: Client,
) -> None:
    """User-search should be admin-only and return empty results for short terms."""
    club = Club.objects.create(name="Search Club")

    admin_user = get_user_model().objects.create_user(
        username="club_admin",
        password="pass1234",  # noqa: S106  # nosec
    )
    club.admin.add(admin_user.player)

    viewer = get_user_model().objects.create_user(
        username="viewer",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(viewer)
    response_forbidden = client.get(
        f"/api/club/clubs/{club.id_uuid}/settings/user-search/",
        {"search": "ad"},
    )
    assert response_forbidden.status_code == HTTPStatus.FORBIDDEN

    client.force_login(admin_user)
    response_short = client.get(
        f"/api/club/clubs/{club.id_uuid}/settings/user-search/",
        {"search": "a"},
    )
    assert response_short.status_code == HTTPStatus.OK
    assert response_short.json() == {"results": []}

    member_user = get_user_model().objects.create_user(
        username="member_user",
        password="pass1234",  # noqa: S106  # nosec
    )
    member_player = member_user.player

    response_hit = client.get(
        f"/api/club/clubs/{club.id_uuid}/settings/user-search/",
        {"search": "member"},
    )
    assert response_hit.status_code == HTTPStatus.OK
    results = response_hit.json()["results"]
    assert any(row["username"] == "member_user" for row in results)

    member_row = next(row for row in results if row["username"] == "member_user")
    assert member_row["player_id"] == str(member_player.id_uuid)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_admin_add_membership_duplicate_active_returns_400(
    client: Client,
) -> None:
    """Adding the same active membership twice should return a validation error."""
    club = Club.objects.create(name="Dup Membership Club")
    admin_user = get_user_model().objects.create_user(
        username="club_admin",
        password="pass1234",  # noqa: S106  # nosec
    )
    club.admin.add(admin_user.player)

    member_user = get_user_model().objects.create_user(
        username="member",
        password="pass1234",  # noqa: S106  # nosec
    )
    _member_player = member_user.player

    client.force_login(admin_user)
    response_first = client.post(
        f"/api/club/clubs/{club.id_uuid}/memberships/",
        data={"username": "member"},
        content_type="application/json",
    )
    assert response_first.status_code == HTTPStatus.CREATED

    response_second = client.post(
        f"/api/club/clubs/{club.id_uuid}/memberships/",
        data={"username": "member"},
        content_type="application/json",
    )
    assert response_second.status_code == HTTPStatus.BAD_REQUEST
    assert (
        response_second.json()["detail"]
        == "Player is already an active member of this club."
    )


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_admin_add_membership_unknown_user_returns_400(client: Client) -> None:
    """Admins should get a helpful 400 when adding an unknown user/player."""
    club = Club.objects.create(name="Missing User Club")
    admin_user = get_user_model().objects.create_user(
        username="club_admin",
        password="pass1234",  # noqa: S106  # nosec
    )
    club.admin.add(admin_user.player)
    client.force_login(admin_user)

    response = client.post(
        f"/api/club/clubs/{club.id_uuid}/memberships/",
        data={"username": "does_not_exist"},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Player/user not found."


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_admin_remove_membership_missing_returns_404(client: Client) -> None:
    """Removing a non-existent active membership should return 404."""
    club = Club.objects.create(name="Remove Missing Club")
    admin_user = get_user_model().objects.create_user(
        username="club_admin",
        password="pass1234",  # noqa: S106  # nosec
    )
    club.admin.add(admin_user.player)

    member_user = get_user_model().objects.create_user(
        username="member",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(admin_user)

    response = client.delete(
        f"/api/club/clubs/{club.id_uuid}/memberships/{member_user.player.id_uuid}/"
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == "Active membership not found."


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_eligibility_dashboard_requires_admin(client: Client) -> None:
    """Eligibility dashboard should be visible only for club admins."""
    club = Club.objects.create(name="Eligibility Club")
    viewer_user = get_user_model().objects.create_user(
        username="elig_viewer",
        password="pass1234",  # noqa: S106  # nosec
    )

    client.force_login(viewer_user)
    response = client.get(f"/api/club/clubs/{club.id_uuid}/eligibility-dashboard/")
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_eligibility_dashboard_returns_own_team_and_distances(
    client: Client,
) -> None:
    """Dashboard should compute own team and lock distances for a player."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025/2026",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )
    club = Club.objects.create(name="Eligibility Club")
    opponent_club = Club.objects.create(name="Opponents")
    opponent_team = Team.objects.create(name="Opp", club=opponent_club)

    team_1 = Team.objects.create(name="1", club=club)
    team_2 = Team.objects.create(name="2", club=club)

    TeamData.objects.create(
        team=team_1,
        season=season,
        competition="",
        wedstrijd_sport=True,
        team_rank=1,
    )
    team_2_data = TeamData.objects.create(
        team=team_2,
        season=season,
        competition="",
        wedstrijd_sport=True,
        team_rank=2,
    )

    admin_user = get_user_model().objects.create_user(
        username="elig_admin",
        password="pass1234",  # noqa: S106  # nosec
    )
    club.admin.add(admin_user.player)

    player_user = get_user_model().objects.create_user(
        username="elig_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = player_user.player
    team_2_data.players.add(player)

    match_a = Match.objects.create(
        home_team=team_2,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now() - timedelta(days=21),
    )
    match_b = Match.objects.create(
        home_team=team_2,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now() - timedelta(days=14),
    )
    match_c = Match.objects.create(
        home_team=team_1,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now() - timedelta(days=7),
    )

    for match in (match_a, match_b, match_c):
        md = MatchData.objects.get(match_link=match)
        md.status = "finished"
        md.save(update_fields=["status"])

        PlayerMatchMinutes.objects.update_or_create(
            match_data=md,
            player=player,
            algorithm_version=LATEST_MATCH_MINUTES_VERSION,
            defaults={"minutes_played": Decimal("120.0")},
        )

    client.force_login(admin_user)
    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/eligibility-dashboard/",
        {"season": str(season.id_uuid)},
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert payload["season_id"] == str(season.id_uuid)
    assert payload["players"]

    player_row = next(
        row
        for row in payload["players"]
        if row["player"]["id_uuid"] == str(player.id_uuid)
    )
    assert player_row["played_matches_count"] == EXPECTED_PLAYED_MATCHES
    assert player_row["restrictions_active"] is True
    assert player_row["own_team_id"] == str(team_2.id_uuid)

    team_1_row = next(
        row for row in player_row["by_team"] if row["team_id"] == str(team_1.id_uuid)
    )
    team_2_row = next(
        row for row in player_row["by_team"] if row["team_id"] == str(team_2.id_uuid)
    )
    assert team_2_row["allowed_for_team"] is True
    assert team_2_row["distance_to_lock"] == 0
    assert team_1_row["distance_to_lock"] >= 1


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_eligibility_dashboard_enforces_lower_team_limit_until_three_quarters(
    client: Client,
) -> None:
    """Only two players from the nearest higher A-team may play one team lower."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2026/2027",
        start_date=today - timedelta(days=10),
        end_date=today + timedelta(days=300),
    )
    club = Club.objects.create(name="Limit Club")
    opponent_club = Club.objects.create(name="Opp")
    opponent_team = Team.objects.create(name="Opp Team", club=opponent_club)

    team_2 = Team.objects.create(name="2", club=club)
    team_3 = Team.objects.create(name="3", club=club)

    team_2_data = TeamData.objects.create(
        team=team_2,
        season=season,
        competition="Reserve 2e klasse",
        wedstrijd_sport=True,
        team_rank=2,
    )
    TeamData.objects.create(
        team=team_3,
        season=season,
        competition="Reserve 3e klasse",
        wedstrijd_sport=True,
        team_rank=3,
    )

    admin_user = get_user_model().objects.create_user(
        username="limit_admin",
        password="pass1234",  # noqa: S106  # nosec
    )
    club.admin.add(admin_user.player)

    players = []
    for index in range(1, 4):
        user = get_user_model().objects.create_user(
            username=f"limit_player_{index}",
            password="pass1234",  # noqa: S106  # nosec
        )
        players.append(user.player)
        team_2_data.players.add(user.player)

    base_days = [25, 18, 11]
    for idx, player in enumerate(players):
        match_team_3 = Match.objects.create(
            home_team=team_3,
            away_team=opponent_team,
            season=season,
            start_time=timezone.now() - timedelta(days=base_days[idx]),
        )
        match_team_2_a = Match.objects.create(
            home_team=team_2,
            away_team=opponent_team,
            season=season,
            start_time=timezone.now() - timedelta(days=base_days[idx] - 3),
        )
        match_team_2_b = Match.objects.create(
            home_team=team_2,
            away_team=opponent_team,
            season=season,
            start_time=timezone.now() - timedelta(days=base_days[idx] - 6),
        )

        for match in (match_team_3, match_team_2_a, match_team_2_b):
            md = MatchData.objects.get(match_link=match)
            md.status = "finished"
            md.save(update_fields=["status"])

            PlayerMatchMinutes.objects.update_or_create(
                match_data=md,
                player=player,
                algorithm_version=LATEST_MATCH_MINUTES_VERSION,
                defaults={"minutes_played": Decimal("120.0")},
            )

    client.force_login(admin_user)
    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/eligibility-dashboard/",
        {"season": str(season.id_uuid)},
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    rows = {
        row["player"]["username"]: row
        for row in payload["players"]
        if row["player"]["username"].startswith("limit_player_")
    }

    player_1_team_3 = next(
        row
        for row in rows["limit_player_1"]["by_team"]
        if row["team_id"] == str(team_3.id_uuid)
    )
    player_2_team_3 = next(
        row
        for row in rows["limit_player_2"]["by_team"]
        if row["team_id"] == str(team_3.id_uuid)
    )
    player_3_team_3 = next(
        row
        for row in rows["limit_player_3"]["by_team"]
        if row["team_id"] == str(team_3.id_uuid)
    )

    assert player_1_team_3["allowed_for_team"] is True
    assert player_2_team_3["allowed_for_team"] is True
    assert player_3_team_3["allowed_for_team"] is False


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_eligibility_dashboard_counts_lowest_a_team_per_speelweek(
    client: Client,
) -> None:
    """In one week with multiple A matches, the lowest A team should count."""
    today = timezone.localdate()
    season = Season.objects.create(
        name="2027/2028",
        start_date=today - timedelta(days=60),
        end_date=today + timedelta(days=300),
    )
    club = Club.objects.create(name="Speelweek Club")
    opponent_club = Club.objects.create(name="Opp Speelweek")
    opponent_team = Team.objects.create(name="Opp SW", club=opponent_club)

    team_1 = Team.objects.create(name="1", club=club)
    team_2 = Team.objects.create(name="2", club=club)

    TeamData.objects.create(
        team=team_1,
        season=season,
        competition="Reserve 2e klasse",
        wedstrijd_sport=True,
        team_rank=1,
    )
    team_2_data = TeamData.objects.create(
        team=team_2,
        season=season,
        competition="Reserve 3e klasse",
        wedstrijd_sport=True,
        team_rank=2,
    )

    admin_user = get_user_model().objects.create_user(
        username="speelweek_admin",
        password="pass1234",  # noqa: S106  # nosec
    )
    club.admin.add(admin_user.player)

    player_user = get_user_model().objects.create_user(
        username="speelweek_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = player_user.player
    team_2_data.players.add(player)

    week_start = today - timedelta(days=(today.isoweekday() - 2) % 7)
    dt_week1 = timezone.make_aware(
        datetime.combine(week_start - timedelta(days=7), time(20, 0)),
    )
    dt_week2_a = timezone.make_aware(
        datetime.combine(week_start + timedelta(days=1), time(19, 30)),
    )
    dt_week2_b = timezone.make_aware(
        datetime.combine(week_start + timedelta(days=3), time(20, 0)),
    )
    dt_week3 = timezone.make_aware(
        datetime.combine(week_start + timedelta(days=8), time(20, 0)),
    )

    matches = [
        Match.objects.create(
            home_team=team_1,
            away_team=opponent_team,
            season=season,
            start_time=dt_week1,
        ),
        Match.objects.create(
            home_team=team_1,
            away_team=opponent_team,
            season=season,
            start_time=dt_week2_a,
        ),
        Match.objects.create(
            home_team=team_2,
            away_team=opponent_team,
            season=season,
            start_time=dt_week2_b,
        ),
        Match.objects.create(
            home_team=team_2,
            away_team=opponent_team,
            season=season,
            start_time=dt_week3,
        ),
    ]

    for match in matches:
        md = MatchData.objects.get(match_link=match)
        md.status = "finished"
        md.save(update_fields=["status"])
        PlayerMatchMinutes.objects.update_or_create(
            match_data=md,
            player=player,
            algorithm_version=LATEST_MATCH_MINUTES_VERSION,
            defaults={"minutes_played": Decimal("120.0")},
        )

    client.force_login(admin_user)
    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/eligibility-dashboard/",
        {"season": str(season.id_uuid)},
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    player_row = next(
        row
        for row in payload["players"]
        if row["player"]["id_uuid"] == str(player.id_uuid)
    )
    assert player_row["played_matches_count"] == EXPECTED_PLAYED_MATCHES
    assert player_row["own_team_id"] == str(team_2.id_uuid)
