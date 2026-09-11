"""Recorded-shape form requests using synthetic HTTP responses only."""

from unittest.mock import Mock

import pytest
import requests

from apps.competition.adapters.outbound.match_forms import SportlinkMatchForms
from apps.competition.adapters.outbound.sportlink import BASE_URL, SportlinkClient
from apps.competition.application.match_forms import MatchFormError
from apps.competition.application.ports import ProviderCooldownError


def response(data: dict, *, status: int = 200, etag: str = "") -> Mock:
    """Provide just the response contract consumed by the transport."""
    return Mock(
        status_code=status, headers={"ETag": etag}, json=Mock(return_value=data)
    )


def test_put_has_side_version_csrf_independent_oauth_and_conditional_header() -> None:
    """Reuse server-held OAuth and verify the replacement with a fresh GET."""
    client = SportlinkClient("synthetic-token", user_agent="synthetic-user-agent")
    original = {"PublicMatchId": "M1", "value": 1}
    updated = {**original, "value": 2}
    client.session.request = Mock(
        side_effect=[
            response(original, etag='"v1"'),
            response(updated),
            response(updated),
        ]
    )
    forms = SportlinkMatchForms(client, Mock())
    assert forms.replace("players", "M1", original, updated, home=False) == updated
    call = client.session.request.call_args_list[1]
    assert call.args == ("PUT", BASE_URL + "matchform/MatchFormTeamPersonsForm")
    assert call.kwargs["params"] == {"PublicMatchId": "M1", "v": "4", "IsHome": "false"}
    assert call.kwargs["headers"] == {"X-Navajo-Version": "4", "If-Match": '"v1"'}
    assert call.kwargs["json"] == updated
    assert call.kwargs["allow_redirects"] is False
    assert client.session.headers["X-Navajo-Instance"] == "KNKV"


def test_intervening_provider_change_stops_before_put() -> None:
    """A new server form must not be overwritten by the queued snapshot."""
    client = SportlinkClient("synthetic", user_agent="synthetic")
    client.session.request = Mock(
        return_value=response({"PublicMatchId": "M1", "value": "other edit"})
    )
    with pytest.raises(MatchFormError, match="knkv_changed"):
        SportlinkMatchForms(client, Mock()).replace(
            "events",
            "M1",
            {"PublicMatchId": "M1"},
            {"PublicMatchId": "M1", "value": "ours"},
        )
    client.session.request.assert_called_once()
    assert client.session.request.call_args.args[0] == "GET"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "knkv_access_denied"),
        (403, "knkv_access_denied"),
        (412, "knkv_changed"),
        (500, "knkv_unavailable"),
    ],
)
def test_http_errors_never_expose_provider_body(status: int, code: str) -> None:
    """Return bounded errors without forwarding private upstream responses."""
    client = SportlinkClient("synthetic", user_agent="synthetic")
    client.session.request = Mock(
        return_value=response({"private": "never return"}, status=status)
    )
    with pytest.raises(MatchFormError, match=code):
        SportlinkMatchForms(client, Mock()).read("players", "M1", home=True)


def test_semantic_error_in_success_status_is_not_a_receipt() -> None:
    """HTTP success alone does not mean KNKV accepted a form."""
    client = SportlinkClient("synthetic", user_agent="synthetic")
    client.session.request = Mock(
        return_value=response({"PublicMatchId": "M1", "Error": {"message": "private"}})
    )
    with pytest.raises(MatchFormError, match="invalid_response"):
        SportlinkMatchForms(client, Mock()).read("events", "M1")


def test_provider_cooldown_is_retained_and_timeout_is_ambiguous() -> None:
    """Rate limits apply globally; failed writes are reconciled rather than replayed."""
    client = SportlinkClient("synthetic", user_agent="synthetic")
    limited = response({}, status=429)
    limited.headers["Retry-After"] = "120"
    client.session.request = Mock(return_value=limited)
    with pytest.raises(ProviderCooldownError) as error:
        SportlinkMatchForms(client, Mock()).read("events", "M1")
    assert error.value.seconds == int(limited.headers["Retry-After"])
    client.session.request.side_effect = requests.Timeout("private transport detail")
    with pytest.raises(MatchFormError, match="connection_failed"):
        SportlinkMatchForms(client, Mock()).read("events", "M1")
