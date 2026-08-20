"""Regression tests for staff schedule management endpoints."""

from __future__ import annotations

from datetime import UTC, date, datetime
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
import pytest

from apps.club.models import Club
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_season_editor_access_reports_staff_capability(client: Client) -> None:
    """The public capability probe reveals no account details."""
    anonymous_response = client.get("/api/seasons/access/")
    assert anonymous_response.status_code == HTTPStatus.OK
    assert anonymous_response.json() == {"can_manage": False}

    staff = get_user_model().objects.create_user(
        username="schedule_staff",
        password="pass1234",  # nosec
        is_staff=True,
    )
    client.force_login(staff)

    staff_response = client.get("/api/seasons/access/")
    assert staff_response.status_code == HTTPStatus.OK
    assert staff_response.json() == {"can_manage": True}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_season_editor_requires_staff_and_validates_dates(client: Client) -> None:
    """Only staff can manage seasons and inverted ranges are rejected."""
    user = get_user_model().objects.create_user(
        username="schedule_viewer",
        password="pass1234",  # nosec
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
@override_settings(SECURE_SSL_REDIRECT=False)
def test_staff_can_create_and_quick_edit_matches(client: Client) -> None:
    """Match writes support the exact fields used by the season editor."""
    staff = get_user_model().objects.create_user(
        username="match_staff",
        password="pass1234",  # nosec
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

    delete_response = client.delete(f"/api/matches/{match.id_uuid}/")
    assert delete_response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
