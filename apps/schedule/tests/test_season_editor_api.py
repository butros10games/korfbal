"""Regression tests for staff schedule management endpoints."""

from __future__ import annotations

from datetime import UTC, date, datetime
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test.client import Client
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, MatchLiveChange, Shot
from apps.schedule.models import Match, Season, SeasonPool
from apps.team.models import Team


@pytest.mark.django_db
def test_season_editor_access_reports_staff_capability(client: Client) -> None:
    """The public capability probe reveals no account details."""
    anonymous_response = client.get("/api/seasons/access/")
    assert anonymous_response.status_code == HTTPStatus.OK
    assert anonymous_response.json() == {"can_manage": False}

    staff = get_user_model().objects.create_user(
        username="schedule_staff",
        is_staff=True,
    )
    client.force_login(staff)

    staff_response = client.get("/api/seasons/access/")
    assert staff_response.status_code == HTTPStatus.OK
    assert staff_response.json() == {"can_manage": True}


@pytest.mark.django_db
def test_season_editor_requires_staff_and_validates_dates(client: Client) -> None:
    """Only staff can manage seasons and inverted ranges are rejected."""
    user = get_user_model().objects.create_user(
        username="schedule_viewer",
    )
    client.force_login(user)
    forbidden = client.get("/api/seasons/")
    assert forbidden.status_code == HTTPStatus.FORBIDDEN

    user.is_staff = True
    user.save(update_fields=["is_staff"])

    invalid = client.post(
        "/api/seasons/",
        data={
            "name": "2026/2027",
            "start_date": "2027-06-01",
            "end_date": "2026-08-01",
        },
        content_type="application/json",
    )
    assert invalid.status_code == HTTPStatus.BAD_REQUEST
    assert "end_date" in invalid.json()

    created = client.post(
        "/api/seasons/",
        data={
            "name": "2026/2027",
            "start_date": "2026-08-01",
            "end_date": "2027-06-01",
        },
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED
    payload = created.json()
    assert payload["match_count"] == 0
    assert payload["is_current"] is True

    updated = client.patch(
        f"/api/seasons/{payload['id_uuid']}/",
        data={"name": "Seizoen 2026/2027"},
        content_type="application/json",
    )
    assert updated.status_code == HTTPStatus.OK
    assert updated.json()["name"] == "Seizoen 2026/2027"


@pytest.mark.django_db
def test_staff_can_create_and_quick_edit_matches(client: Client) -> None:
    """Match writes support the exact fields used by the season editor."""
    staff = get_user_model().objects.create_user(
        username="match_staff",
        is_staff=True,
    )
    client.force_login(staff)

    season = Season.objects.create(
        name="Editor season",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 6, 1),
    )
    home_club = Club.objects.create(name="Home club")
    away_club = Club.objects.create(name="Away club")
    home_team = Team.objects.create(name="1", club=home_club)
    away_team = Team.objects.create(name="2", club=away_club)
    replacement_team = Team.objects.create(name="3", club=away_club)
    start_time = datetime(2026, 9, 12, 19, 30, tzinfo=UTC)

    created = client.post(
        "/api/matches/",
        data={
            "season_id": str(season.id_uuid),
            "home_team_id": str(home_team.id_uuid),
            "away_team_id": str(away_team.id_uuid),
            "start_time": start_time.isoformat(),
        },
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED
    payload = created.json()
    assert payload["season_id"] == str(season.id_uuid)
    assert payload["home_team"]["id_uuid"] == str(home_team.id_uuid)
    assert payload["away_team"]["id_uuid"] == str(away_team.id_uuid)

    match = Match.objects.get(id_uuid=payload["id_uuid"])
    edited_time = datetime(2026, 9, 13, 20, 15, tzinfo=UTC)
    updated = client.patch(
        f"/api/matches/{match.id_uuid}/",
        data={
            "away_team_id": str(replacement_team.id_uuid),
            "start_time": edited_time.isoformat(),
        },
        content_type="application/json",
    )
    assert updated.status_code == HTTPStatus.OK
    assert updated.json()["away_team"]["id_uuid"] == str(replacement_team.id_uuid)

    invalid = client.patch(
        f"/api/matches/{match.id_uuid}/",
        data={"away_team_id": str(home_team.id_uuid)},
        content_type="application/json",
    )
    assert invalid.status_code == HTTPStatus.BAD_REQUEST
    assert "away_team_id" in invalid.json()

    staff.is_staff = False
    staff.save(update_fields=["is_staff"])
    forbidden_delete = client.delete(f"/api/matches/{match.id_uuid}/")
    assert forbidden_delete.status_code == HTTPStatus.FORBIDDEN
    assert Match.objects.filter(id_uuid=match.id_uuid).exists()

    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    delete_response = client.delete(f"/api/matches/{match.id_uuid}/")
    assert delete_response.status_code == HTTPStatus.NO_CONTENT
    assert not Match.objects.filter(id_uuid=match.id_uuid).exists()


@pytest.mark.django_db(transaction=True)
def test_staff_can_delete_match_with_tracker_history(client: Client) -> None:
    """Deleting a tracked match does not recreate live rows during its cascade."""
    staff = get_user_model().objects.create_user(
        username="tracked_match_staff",
        is_staff=True,
    )
    client.force_login(staff)

    season = Season.objects.create(
        name="Tracked season",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 6, 1),
    )
    home_team = Team.objects.create(
        name="1",
        club=Club.objects.create(name="Tracked home club"),
    )
    away_team = Team.objects.create(
        name="2",
        club=Club.objects.create(name="Tracked away club"),
    )
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=datetime(2026, 8, 29, 19, 30, tzinfo=UTC),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])
    shooter = get_user_model().objects.create_user(username="tracked_shooter")
    for _index in range(3):
        Shot.objects.create(
            player=shooter.player,
            match_data=match_data,
            team=home_team,
            scored=True,
            time=datetime(2026, 8, 22, 19, 30, tzinfo=UTC),
        )

    match_data_id = match_data.id_uuid
    assert MatchLiveChange.objects.filter(match_data_id=match_data_id).exists()

    response = client.delete(f"/api/matches/{match.id_uuid}/")

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert not Match.objects.filter(id_uuid=match.id_uuid).exists()
    assert not MatchData.objects.filter(id_uuid=match_data_id).exists()
    assert not Shot.objects.filter(match_data_id=match_data_id).exists()
    assert not MatchLiveChange.objects.filter(match_data_id=match_data_id).exists()


@pytest.mark.django_db
def test_staff_can_manage_season_pools(client: Client) -> None:
    """Staff can create and edit a season pool with its team membership."""
    staff = get_user_model().objects.create_user(
        username="pool_staff",
        is_staff=True,
    )
    client.force_login(staff)
    season = Season.objects.create(
        name="Pool season",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 6, 1),
    )
    club = Club.objects.create(name="Pool club")
    teams = [Team.objects.create(name=str(number), club=club) for number in range(1, 4)]

    created = client.post(
        "/api/seasons/pools/",
        data={
            "season_id": str(season.id_uuid),
            "name": "Poule A",
            "team_ids": [str(teams[0].id_uuid), str(teams[1].id_uuid)],
        },
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED
    payload = created.json()
    assert payload["season_id"] == str(season.id_uuid)
    assert payload["name"] == "Poule A"
    assert {team["id_uuid"] for team in payload["teams"]} == {
        str(teams[0].id_uuid),
        str(teams[1].id_uuid),
    }
    assert payload["match_count"] == 0

    listed = client.get(f"/api/seasons/pools/?season={season.id_uuid}")
    assert listed.status_code == HTTPStatus.OK
    assert len(listed.json()) == 1

    updated = client.patch(
        f"/api/seasons/pools/{payload['id_uuid']}/",
        data={"team_ids": [str(team.id_uuid) for team in teams]},
        content_type="application/json",
    )
    assert updated.status_code == HTTPStatus.OK
    assert len(updated.json()["teams"]) == len(teams)

    duplicate_membership = client.post(
        "/api/seasons/pools/",
        data={
            "season_id": str(season.id_uuid),
            "name": "Poule B",
            "team_ids": [str(teams[0].id_uuid), str(teams[1].id_uuid)],
        },
        content_type="application/json",
    )
    assert duplicate_membership.status_code == HTTPStatus.BAD_REQUEST
    assert "team_ids" in duplicate_membership.json()


@pytest.mark.django_db
def test_pooled_matches_require_teams_from_the_same_season_pool(client: Client) -> None:
    """Pool assignment constrains both the season and the participating teams."""
    staff = get_user_model().objects.create_user(
        username="pooled_match_staff",
        is_staff=True,
    )
    client.force_login(staff)
    season = Season.objects.create(
        name="Pooled match season",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 6, 1),
    )
    other_season = Season.objects.create(
        name="Other pool season",
        start_date=date(2027, 8, 1),
        end_date=date(2028, 6, 1),
    )
    club = Club.objects.create(name="Pooled match club")
    home_team = Team.objects.create(name="1", club=club)
    away_team = Team.objects.create(name="2", club=club)
    outside_team = Team.objects.create(name="3", club=club)
    pool = SeasonPool.objects.create(season=season, name="Poule A")
    pool.teams.set([home_team, away_team])
    start_time = datetime(2026, 9, 12, 19, 30, tzinfo=UTC)

    wrong_season = client.post(
        "/api/matches/",
        data={
            "season_id": str(other_season.id_uuid),
            "pool_id": str(pool.id_uuid),
            "home_team_id": str(home_team.id_uuid),
            "away_team_id": str(away_team.id_uuid),
            "start_time": start_time.isoformat(),
        },
        content_type="application/json",
    )
    assert wrong_season.status_code == HTTPStatus.BAD_REQUEST
    assert "pool_id" in wrong_season.json()

    wrong_team = client.post(
        "/api/matches/",
        data={
            "season_id": str(season.id_uuid),
            "pool_id": str(pool.id_uuid),
            "home_team_id": str(home_team.id_uuid),
            "away_team_id": str(outside_team.id_uuid),
            "start_time": start_time.isoformat(),
        },
        content_type="application/json",
    )
    assert wrong_team.status_code == HTTPStatus.BAD_REQUEST
    assert "away_team_id" in wrong_team.json()

    created = client.post(
        "/api/matches/",
        data={
            "season_id": str(season.id_uuid),
            "pool_id": str(pool.id_uuid),
            "home_team_id": str(home_team.id_uuid),
            "away_team_id": str(away_team.id_uuid),
            "start_time": start_time.isoformat(),
        },
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED
    assert created.json()["pool_id"] == str(pool.id_uuid)
    assert created.json()["pool_name"] == "Poule A"

    remove_used_team = client.patch(
        f"/api/seasons/pools/{pool.id_uuid}/",
        data={"team_ids": [str(home_team.id_uuid), str(outside_team.id_uuid)]},
        content_type="application/json",
    )
    assert remove_used_team.status_code == HTTPStatus.BAD_REQUEST
    assert "team_ids" in remove_used_team.json()
