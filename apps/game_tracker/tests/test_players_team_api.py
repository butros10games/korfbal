"""Tests for the available-players team endpoint."""

from http import HTTPStatus

from django.test.client import Client
import pytest

from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    login_home_club_editor,
)


pytestmark = pytest.mark.django_db


def _players_team_url(match_id: object, team_id: object) -> str:
    return f"/api/match/players_team/{match_id}/{team_id}/"


def test_players_team_returns_empty_when_team_data_missing(client: Client) -> None:
    """Missing optional TeamData yields an empty player list."""
    tracker = create_tracker_match(prefix="Players Team Missing Data")
    login_home_club_editor(client, tracker, "players_team_editor")

    response = client.get(
        _players_team_url(tracker.match.id_uuid, tracker.home_team.id_uuid)
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"players": []}


def test_players_team_rejects_anonymous_users_with_json(client: Client) -> None:
    """Anonymous requests receive an API error rather than a login redirect."""
    tracker = create_tracker_match(prefix="Players Team Anonymous")

    response = client.get(
        _players_team_url(tracker.match.id_uuid, tracker.home_team.id_uuid)
    )

    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
    assert response.headers["Content-Type"].startswith("application/json")
