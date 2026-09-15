"""HTTP classification of private match-form application failures."""

from unittest.mock import Mock

from django.contrib.auth.models import User
from django.test import Client
import pytest

from apps.competition.api.match_forms import MatchFormView
from apps.competition.application.match_forms import MatchFormError


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("not_connected", 404),
        ("access_denied", 403),
        ("invalid_action", 400),
        ("captain_required", 400),
        ("captain_not_selected", 400),
        ("match_started", 409),
        ("team_not_linked", 409),
        ("players_not_linked", 409),
        ("substitutions_not_enabled", 409),
    ],
)
def test_match_form_failure_status_and_message(
    client: Client, monkeypatch: pytest.MonkeyPatch, code: str, expected_status: int
) -> None:
    """Access, input, missing-resource and state errors are distinct at the adapter."""
    client.force_login(User.objects.create_user(username="match-form-error"))
    monkeypatch.setattr(MatchFormView, "_access", Mock(return_value=object()))
    monkeypatch.setattr(
        "apps.competition.api.match_forms.enqueue",
        Mock(side_effect=MatchFormError(code)),
    )
    identifier = "11111111-1111-4111-8111-111111111111"
    response = client.post(
        f"/api/competition/match-forms/{identifier}/{identifier}/",
        {"action": "publish", "expected_revision": 0},
        content_type="application/json",
    )
    assert response.status_code == expected_status
    assert response.json()["code"] == code
    assert response.json()["detail"] != code
    assert response.json()["message"] == response.json()["detail"]
