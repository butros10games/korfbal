"""Anonymous tracker links retain match/team scope, expiry and CSRF boundaries."""

from datetime import timedelta
from http import HTTPStatus
from unittest.mock import patch

from bg_auth.jwt import issue_access_token
from django.http import HttpResponseBase
from django.test import Client
from django.utils import timezone
import pytest

from apps.game_tracker.models import (
    MatchPlayer,
    PlayerGroup,
    TrackerAccessLink,
    TrackerCommand,
)
from apps.game_tracker.services.tracker_access import SESSION_KEY
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_tracker_player,
)
from apps.team.models import TeamData

from .match_api_test_support import MatchGraph, create_match_graph, create_user
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
            HTTP_ORIGIN="https://untrusted.example",
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
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


@pytest.fixture
def guest_lineup(
    invitation: tuple[MatchGraph, str],
) -> tuple[MatchGraph, Client, PlayerGroup, PlayerGroup]:
    """Redeem a link with a player available for lineup configuration."""
    graph, token = invitation
    create_group_types("Aanval", "Verdediging", "Reserve")
    guest = Client()
    assert redeem(guest, graph, token).status_code == HTTPStatus.NO_CONTENT
    reserve = PlayerGroup.objects.get(
        match_data=graph.match_data, team=graph.home_team, starting_type__name="Reserve"
    )
    attack = PlayerGroup.objects.get(
        match_data=graph.match_data, team=graph.home_team, starting_type__name="Aanval"
    )
    player = create_tracker_player(username="Guest lineup player")
    team_data, _ = TeamData.objects.get_or_create(
        team=graph.home_team, season=graph.match.season
    )
    team_data.players.add(player)
    reserve.players.add(player)
    return graph, guest, reserve, attack


def test_guest_configures_lineup_and_tracker_reads_saved_groups(
    guest_lineup: tuple[MatchGraph, Client, PlayerGroup, PlayerGroup],
) -> None:
    """Guest edits persist through the regular revision boundary into tracker state."""
    graph, guest, reserve, attack = guest_lineup
    player = reserve.players.get()
    for endpoint in ("players_team", "player_search"):
        response = guest.get(
            f"/api/match/{endpoint}/{graph.match.pk}/{graph.home_team.pk}/",
            {"search": "Guest"},
        )
        assert response.status_code == HTTPStatus.OK
    revision = guest.get(
        f"/api/match/player_overview_data/{graph.match.pk}/{graph.home_team.pk}/"
    ).json()["live_revision"]
    payload = {
        "players": [{"id_uuid": str(player.pk), "groupId": str(reserve.pk)}],
        "new_group_id": str(attack.pk),
        "expected_revision": revision,
    }
    response = guest.post("/api/match/player_designation/", payload, content_type=JSON)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["live_revision"] == revision + 1
    assert attack.players.filter(pk=player.pk).exists()
    assert not reserve.players.filter(pk=player.pk).exists()
    assert MatchPlayer.objects.filter(
        match_data=graph.match_data, team=graph.home_team, player=player
    ).exists()
    state = guest.get(_url(graph, "state")).json()
    assert state["live_revision"] == revision + 1
    assert str(player.pk) in str(state["player_groups"])
    stale = guest.post("/api/match/player_designation/", payload, content_type=JSON)
    assert stale.status_code == HTTPStatus.CONFLICT
    assert stale.json()["code"] == "revision_conflict"


@pytest.mark.parametrize(
    "scope", ["opponent", "other_match", "mixed", "revoked", "expired", "rotated"]
)
def test_guest_lineup_grants_cannot_escape_scope(
    guest_lineup: tuple[MatchGraph, Client, PlayerGroup, PlayerGroup], scope: str
) -> None:
    """Source and destination groups must both belong to the live invitation."""
    graph, guest, reserve, attack = guest_lineup
    source = reserve
    if scope in {"opponent", "mixed"}:
        attack = PlayerGroup.objects.get(
            match_data=graph.match_data,
            team=graph.away_team,
            starting_type__name="Aanval",
        )
        if scope == "opponent":
            source = PlayerGroup.objects.get(
                match_data=graph.match_data,
                team=graph.away_team,
                starting_type__name="Reserve",
            )
    elif scope == "other_match":
        other = create_match_graph(prefix="Other lineup")
        attack = PlayerGroup.objects.get(
            match_data=other.match_data,
            team=other.home_team,
            starting_type__name="Aanval",
        )
        source = PlayerGroup.objects.get(
            match_data=other.match_data,
            team=other.home_team,
            starting_type__name="Reserve",
        )
    elif scope == "revoked":
        TrackerAccessLink.objects.all().delete()
    elif scope == "expired":
        TrackerAccessLink.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    else:
        TrackerAccessLink.objects.update(token_hash="rotated")
    payload = {
        "players": [
            {"id_uuid": str(reserve.players.get().pk), "groupId": str(source.pk)}
        ],
        "new_group_id": str(attack.pk),
        "expected_revision": graph.match_data.live_revision,
    }
    response = guest.post("/api/match/player_designation/", payload, content_type=JSON)
    assert response.status_code == (
        HTTPStatus.BAD_REQUEST if scope == "mixed" else HTTPStatus.FORBIDDEN
    )
    assert not attack.players.exists()
    assert reserve.players.count() == 1
    graph.match_data.refresh_from_db()
    assert graph.match_data.live_revision == payload["expected_revision"]
    if scope != "mixed":
        for endpoint in ("players_team", "player_search"):
            assert (
                guest.get(
                    f"/api/match/{endpoint}/{attack.match_data.match_link_id}/{attack.team_id}/",
                    {"search": "Guest"},
                ).status_code
                == HTTPStatus.FORBIDDEN
            )


@pytest.mark.parametrize("signed_in", [False, True])
def test_guest_lineup_write_requires_csrf(
    guest_lineup: tuple[MatchGraph, Client, PlayerGroup, PlayerGroup],
    signed_in: bool,
) -> None:
    """Anonymous designation enforces CSRF even with a valid invitation session."""
    graph, guest, reserve, attack = guest_lineup
    if signed_in:
        guest.force_login(create_user(username="Signed-in invited outsider"))
    guest.handler.enforce_csrf_checks = True
    payload = {
        "players": [
            {"id_uuid": str(reserve.players.get().pk), "groupId": str(reserve.pk)}
        ],
        "new_group_id": str(attack.pk),
        "expected_revision": graph.match_data.live_revision,
    }
    assert (
        guest.post(
            "/api/match/player_designation/", payload, content_type=JSON
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    csrf = guest.get("/auth/session/").json()["csrfToken"]
    assert (
        guest.post(
            "/api/match/player_designation/",
            payload,
            content_type=JSON,
            HTTP_X_CSRFTOKEN=csrf,
            HTTP_ORIGIN="https://untrusted.example",
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    assert (
        guest.post(
            "/api/match/player_designation/",
            payload,
            content_type=JSON,
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == HTTPStatus.OK
    )


@pytest.mark.parametrize("forged_source", [False, True])
def test_guest_cannot_add_a_player_outside_the_invited_club(
    guest_lineup: tuple[MatchGraph, Client, PlayerGroup, PlayerGroup],
    forged_source: bool,
) -> None:
    """Knowing a player's UUID cannot publish them into an unrelated lineup."""
    graph, guest, reserve, attack = guest_lineup
    outsider = create_tracker_player(username="Unrelated private player")
    payload = {
        "players": [
            {
                "id_uuid": str(outsider.pk),
                **({"groupId": str(reserve.pk)} if forged_source else {}),
            }
        ],
        "new_group_id": str(attack.pk if forged_source else reserve.pk),
        "expected_revision": graph.match_data.live_revision,
    }
    response = guest.post("/api/match/player_designation/", payload, content_type=JSON)
    assert response.status_code in {HTTPStatus.BAD_REQUEST, HTTPStatus.FORBIDDEN}
    assert not outsider.player_groups.filter(match_data=graph.match_data).exists()
    graph.match_data.refresh_from_db()
    assert graph.match_data.live_revision == payload["expected_revision"]


def test_guest_cannot_forge_source_group_membership(
    guest_lineup: tuple[MatchGraph, Client, PlayerGroup, PlayerGroup],
) -> None:
    """A club player must actually belong to the source group in a move request."""
    graph, guest, reserve, attack = guest_lineup
    player = reserve.players.get()
    reserve.players.remove(player)
    defense = PlayerGroup.objects.get(
        match_data=graph.match_data,
        team=graph.home_team,
        starting_type__name="Verdediging",
    )
    defense.players.add(player)
    response = guest.post(
        "/api/match/player_designation/",
        {
            "players": [{"id_uuid": str(player.pk), "groupId": str(reserve.pk)}],
            "new_group_id": str(attack.pk),
            "expected_revision": graph.match_data.live_revision,
        },
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert not attack.players.exists()
    assert defense.players.filter(pk=player.pk).exists()


def test_guest_can_add_an_available_club_player(
    guest_lineup: tuple[MatchGraph, Client, PlayerGroup, PlayerGroup],
) -> None:
    """The permitted search-to-reserve flow works without signing in."""
    graph, guest, reserve, _attack = guest_lineup
    player = create_tracker_player(username="Available club player")
    team_data = TeamData.objects.get(team=graph.home_team, season=graph.match.season)
    team_data.players.add(player)
    for endpoint in ("players_team", "player_search"):
        response = guest.get(
            f"/api/match/{endpoint}/{graph.match.pk}/{graph.home_team.pk}/",
            {"search": "Available"},
        )
        assert [row["id_uuid"] for row in response.json()["players"]] == [
            str(player.pk)
        ]
    response = guest.post(
        "/api/match/player_designation/",
        {
            "players": [{"id_uuid": str(player.pk)}],
            "new_group_id": str(reserve.pk),
            "expected_revision": graph.match_data.live_revision,
        },
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.OK
    assert reserve.players.filter(pk=player.pk).exists()


@pytest.mark.parametrize("has_club_permission", [False, True])
def test_bearer_lineup_writes_use_the_token_identity_not_guest_cookies(
    guest_lineup: tuple[MatchGraph, Client, PlayerGroup, PlayerGroup],
    has_club_permission: bool,
) -> None:
    """A bearer token cannot bypass CSRF by borrowing a browser invitation."""
    graph, guest, reserve, attack = guest_lineup
    user = (
        _login_member(Client(), graph, "Authorized bearer editor")
        if has_club_permission
        else create_user(username="Unrelated bearer identity")
    )
    token, _expires_at = issue_access_token(user)
    guest.handler.enforce_csrf_checks = True
    response = guest.post(
        "/api/match/player_designation/",
        {
            "players": [
                {"id_uuid": str(reserve.players.get().pk), "groupId": str(reserve.pk)}
            ],
            "new_group_id": str(attack.pk),
            "expected_revision": graph.match_data.live_revision,
        },
        content_type=JSON,
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert response.status_code == (
        HTTPStatus.OK if has_club_permission else HTTPStatus.FORBIDDEN
    )
    assert attack.players.exists() is has_club_permission
