"""Focused HTTP adapter tests for match tracker routes."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Any, cast
from unittest.mock import patch

from django.contrib.auth.base_user import AbstractBaseUser
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.game_tracker.services.tracker_commands import TrackerCommandError
from apps.player.models.player_club_membership import PlayerClubMembership

from .match_api_test_support import MatchGraph, create_match_graph, create_user


pytestmark = pytest.mark.django_db
STATE_SERVICE = "apps.schedule.api.views.get_tracker_state"
COMMAND_SERVICE = "apps.schedule.api.views.apply_tracker_command"
POLL_SERVICE = "apps.schedule.api.views.poll_tracker_state"
JSON = "application/json"


def _url(graph: MatchGraph, action: str, *, team_id: object | None = None) -> str:
    team_id = team_id or graph.home_team.id_uuid
    return f"/api/matches/{graph.match.id_uuid}/tracker/{team_id}/{action}/"


def _login_member(client: Client, graph: MatchGraph, username: str) -> AbstractBaseUser:
    user = create_user(username=username)
    PlayerClubMembership.objects.create(
        player=cast(Any, user).player,
        club=graph.home_team.club,
        start_date=timezone.localdate(graph.match.start_time) - timedelta(days=1),
    )
    client.force_login(user)
    return user


def test_outsider_cannot_read_tracker_state(client: Client) -> None:
    """Reject an outsider before calling the state service."""
    graph = create_match_graph(prefix="Outsider state")
    client.force_login(create_user(username="state-outsider"))
    with patch(STATE_SERVICE) as get_state:
        response = client.get(_url(graph, "state"))
    assert response.status_code == HTTPStatus.FORBIDDEN
    get_state.assert_not_called()


def test_outsider_cannot_apply_tracker_command(client: Client) -> None:
    """Reject an outsider before dispatching a command."""
    graph = create_match_graph(prefix="Outsider command")
    client.force_login(create_user(username="command-outsider"))
    with patch(COMMAND_SERVICE) as apply_command:
        response = client.post(
            _url(graph, "commands"), {"command": "start/pause"}, content_type=JSON
        )
    assert response.status_code == HTTPStatus.FORBIDDEN
    apply_command.assert_not_called()


def test_club_member_can_read_tracker_state(client: Client) -> None:
    """Forward an authorized state read."""
    graph = create_match_graph(prefix="Member state")
    _login_member(client, graph, "state-member")
    state = {"score": {"for": 1, "against": 2}}
    with patch(STATE_SERVICE, return_value=state) as get_state:
        response = client.get(_url(graph, "state"))
    assert response.status_code == HTTPStatus.OK
    assert response.json() == state
    get_state.assert_called_once_with(graph.match, team=graph.home_team)


def test_club_member_can_apply_tracker_command(client: Client) -> None:
    """Forward member, team, and parsed command payload."""
    graph = create_match_graph(prefix="Member command")
    member = _login_member(client, graph, "command-member")
    command = {"command": "start/pause"}
    state = {"status": "active"}
    with patch(COMMAND_SERVICE, return_value=state) as apply_command:
        response = client.post(_url(graph, "commands"), command, content_type=JSON)
    assert response.status_code == HTTPStatus.OK
    assert response.json() == state
    apply_command.assert_called_once_with(
        graph.match, team=graph.home_team, payload=command, actor=member
    )


def test_tracker_command_rejects_non_object_json(client: Client) -> None:
    """Reject a JSON array before command dispatch."""
    graph = create_match_graph(prefix="Invalid command")
    _login_member(client, graph, "invalid-command-member")
    with patch(COMMAND_SERVICE) as apply_command:
        response = client.post(_url(graph, "commands"), "[]", content_type=JSON)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"detail": "Invalid JSON body."}
    apply_command.assert_not_called()


def test_tracker_conflict_returns_reconciliation_metadata(client: Client) -> None:
    """Retain offline-client reconciliation metadata."""
    graph = create_match_graph(prefix="Command conflict")
    member = _login_member(client, graph, "conflict-member")
    command = {"command": "start/pause"}
    details: dict[str, object] = {
        "client_sequence": 4,
        "command_id": "committed-command",
        "committed_revision": 1,
    }
    conflict = TrackerCommandError(
        "client_sequence was already used by another command.",
        code="client_sequence_conflict",
        details=details,
    )
    with patch(COMMAND_SERVICE, side_effect=conflict) as apply_command:
        response = client.post(_url(graph, "commands"), command, content_type=JSON)
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json() == {
        "detail": "client_sequence was already used by another command.",
        "code": "client_sequence_conflict",
        **details,
    }
    apply_command.assert_called_once_with(
        graph.match, team=graph.home_team, payload=command, actor=member
    )


def test_tracker_poll_rejects_invalid_since_revision(client: Client) -> None:
    """Reject an invalid cursor before polling state."""
    graph = create_match_graph(prefix="Invalid poll")
    _login_member(client, graph, "poll-member")
    with patch(POLL_SERVICE) as poll_state:
        response = client.get(_url(graph, "poll"), {"since_revision": "invalid"})
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"detail": "Invalid 'since_revision'."}
    poll_state.assert_not_called()


def test_unrelated_team_is_rejected_before_state_lookup(client: Client) -> None:
    """Reject a perspective outside the match."""
    graph = create_match_graph(prefix="Team boundary")
    unrelated = create_match_graph(prefix="Unrelated team")
    _login_member(client, graph, "team-boundary-member")
    with patch(STATE_SERVICE) as get_state:
        response = client.get(_url(graph, "state", team_id=unrelated.home_team.id_uuid))
    assert response.status_code == HTTPStatus.FORBIDDEN
    get_state.assert_not_called()
