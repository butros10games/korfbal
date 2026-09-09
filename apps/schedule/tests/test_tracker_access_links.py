"""Anonymous tracker links retain match/team scope, expiry and CSRF boundaries."""

from datetime import timedelta
from http import HTTPStatus
from unittest.mock import patch

from django.http import HttpResponseBase
from django.test import Client
from django.utils import timezone
import pytest

from apps.game_tracker.models import TrackerAccessLink, TrackerCommand
from apps.game_tracker.services.tracker_access import SESSION_KEY

from .match_api_test_support import MatchGraph, create_match_graph
from .test_match_tracker_api import (
    COMMAND_SERVICE,
    JSON,
    POLL_SERVICE,
    _login_member,
    _url,
)


pytestmark = pytest.mark.django_db


@pytest.fixture
def invitation(client: Client) -> tuple[MatchGraph, str]:
    """Issue a link as an authorized club member."""
    graph = create_match_graph(prefix="Shared tracker")
    _login_member(client, graph, "link-owner")
    response = client.post(_url(graph, "access"), {}, content_type=JSON)
    assert response.status_code == HTTPStatus.OK
    return graph, response.json()["token"]


def redeem(guest: Client, graph: MatchGraph, token: str) -> HttpResponseBase:
    """Exchange a secret using a separate browser session."""
    return guest.post(_url(graph, "access/redeem"), {"token": token}, content_type=JSON)


def test_guest_can_track_without_login_and_cannot_escalate(
    invitation: tuple[MatchGraph, str],
) -> None:
    """Guest can track without login and cannot escalate."""
    graph, token = invitation
    guest = Client()
    assert redeem(guest, graph, token).status_code == HTTPStatus.NO_CONTENT
    assert "_auth_user_id" not in guest.session
    assert token not in str(dict(guest.session))
    assert TrackerAccessLink.objects.get().token_hash != token
    state = guest.get(_url(graph, "state"))
    assert state.status_code == HTTPStatus.OK
    with patch(COMMAND_SERVICE, return_value={"live_revision": 1}) as command:
        response = guest.post(
            _url(graph, "commands"), {"command": "start/pause"}, content_type=JSON
        )
    assert response.status_code == HTTPStatus.OK
    assert not command.call_args.kwargs["actor"].is_authenticated
    with patch(POLL_SERVICE, return_value={"changed": False}):
        assert (
            guest.get(_url(graph, "poll"), {"since_revision": 0}).status_code
            == HTTPStatus.OK
        )
    assert (
        guest.get(_url(graph, "state", team_id=graph.away_team.pk)).status_code
        == HTTPStatus.UNAUTHORIZED
    )
    other = create_match_graph(prefix="Other shared tracker")
    assert guest.get(_url(other, "state")).status_code == HTTPStatus.UNAUTHORIZED
    for method in (guest.post, guest.delete):
        assert (
            method(_url(graph, "access"), {}, content_type=JSON).status_code
            == HTTPStatus.FORBIDDEN
        )
    assert guest.get(_url(graph, "access")).json() == {
        "can_share": False,
        "expires_at": None,
    }
    assert (
        guest.post(
            f"/api/matches/{graph.match.pk}/notes/?team={graph.home_team.pk}",
            {},
            content_type=JSON,
        ).status_code
        == HTTPStatus.UNAUTHORIZED
    )


@pytest.mark.parametrize("change", ["revoke", "rotate", "expire"])
def test_changes_invalidate_both_link_and_existing_guest_session(
    client: Client, invitation: tuple[MatchGraph, str], change: str
) -> None:
    """Changes invalidate both link and existing guest session."""
    graph, token = invitation
    guest = Client()
    assert redeem(guest, graph, token).status_code == HTTPStatus.NO_CONTENT
    if change == "revoke":
        assert client.delete(_url(graph, "access")).status_code == HTTPStatus.OK
    elif change == "rotate":
        new_token = client.post(_url(graph, "access"), {}, content_type=JSON).json()[
            "token"
        ]
        assert new_token != token
        assert redeem(Client(), graph, new_token).status_code == HTTPStatus.NO_CONTENT
    else:
        TrackerAccessLink.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    assert guest.get(_url(graph, "state")).status_code == HTTPStatus.UNAUTHORIZED
    assert redeem(Client(), graph, token).status_code == HTTPStatus.FORBIDDEN
    with patch(COMMAND_SERVICE) as command:
        assert (
            guest.post(
                _url(graph, "commands"), {"command": "start/pause"}, content_type=JSON
            ).status_code
            == HTTPStatus.UNAUTHORIZED
        )
    command.assert_not_called()


def test_invalid_and_cross_team_tokens_do_not_grant_access(
    invitation: tuple[MatchGraph, str],
) -> None:
    """Invalid and cross team tokens do not grant access."""
    graph, token = invitation
    guest = Client()
    for invalid in [token + "x", "invalid"]:
        assert redeem(guest, graph, invalid).status_code == HTTPStatus.FORBIDDEN
    assert (
        guest.post(
            _url(graph, "access/redeem", team_id=graph.away_team.pk),
            {"token": token},
            content_type=JSON,
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    assert SESSION_KEY not in guest.session
    assert guest.get(_url(graph, "state")).status_code == HTTPStatus.UNAUTHORIZED


def test_guest_redemption_and_writes_enforce_csrf(
    invitation: tuple[MatchGraph, str],
) -> None:
    """Guest redemption and writes enforce csrf."""
    graph, token = invitation
    guest = Client(enforce_csrf_checks=True)
    assert redeem(guest, graph, token).status_code == HTTPStatus.FORBIDDEN
    session = guest.get("/auth/session/")
    csrf = session.json()["csrfToken"]
    assert (
        guest.post(
            _url(graph, "access/redeem"),
            {"token": token},
            content_type=JSON,
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == HTTPStatus.NO_CONTENT
    )
    with patch(COMMAND_SERVICE, return_value={}) as command:
        assert (
            guest.post(
                _url(graph, "commands"), {"command": "start/pause"}, content_type=JSON
            ).status_code
            == HTTPStatus.FORBIDDEN
        )
        command.assert_not_called()
        assert (
            guest.post(
                _url(graph, "commands"),
                {"command": "start/pause"},
                content_type=JSON,
                HTTP_X_CSRFTOKEN=csrf,
            ).status_code
            == HTTPStatus.OK
        )


def test_anonymous_cannot_issue_link(client: Client) -> None:
    """Anonymous cannot issue link."""
    graph = create_match_graph(prefix="No anonymous sharing")
    assert (
        client.post(_url(graph, "access"), {}, content_type=JSON).status_code
        == HTTPStatus.FORBIDDEN
    )
    assert not TrackerAccessLink.objects.exists()


def test_guest_command_commits_and_preserves_revision_contract(
    invitation: tuple[MatchGraph, str],
) -> None:
    """A guest starts the real match through the same revision/idempotency boundary."""
    graph, token = invitation
    guest = Client()
    assert redeem(guest, graph, token).status_code == HTTPStatus.NO_CONTENT
    state = guest.get(_url(graph, "state")).json()
    payload = {"command": "start/pause", "expected_revision": state["live_revision"]}
    response = guest.post(_url(graph, "commands"), payload, content_type=JSON)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "active"
    graph.match_data.refresh_from_db()
    assert graph.match_data.status == "active"
    assert TrackerCommand.objects.get(match_data=graph.match_data).actor is None
    assert (
        guest.post(_url(graph, "commands"), payload, content_type=JSON).status_code
        == HTTPStatus.CONFLICT
    )


@pytest.mark.parametrize(
    "action", ["access", "access/redeem", "state", "poll", "commands"]
)
def test_malformed_scope_returns_not_found(client: Client, action: str) -> None:
    """Malformed invitation scopes never reach UUID database lookups."""
    graph = create_match_graph(prefix="Invalid tracker scope")
    assert (
        client.post(_url(graph, action, team_id="not-a-uuid")).status_code
        == HTTPStatus.NOT_FOUND
    )
