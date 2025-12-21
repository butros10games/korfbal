"""Tests for the club API endpoints."""

from datetime import timedelta
from http import HTTPStatus
import uuid

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData
from apps.player.models import PlayerClubMembership
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


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
