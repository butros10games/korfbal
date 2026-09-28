"""Regression tests for the public OpenAPI contract."""

from http import HTTPStatus
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
import pytest
import yaml


def test_openapi_schema_has_no_warnings_or_errors(tmp_path: Path) -> None:
    """Keep every API endpoint discoverable and its schema structurally valid."""
    call_command(
        "spectacular",
        validate=True,
        fail_on_warn=True,
        file=str(tmp_path / "openapi.yaml"),
        verbosity=0,
    )

    schema = yaml.safe_load((tmp_path / "openapi.yaml").read_text())
    paths = schema["paths"]
    upcoming = paths["/api/matches/upcoming-page/"]["get"]
    assert {"page", "page_size", "followed", "team", "club", "season"} <= {
        parameter["name"] for parameter in upcoming["parameters"]
    }
    assert upcoming["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PaginatedMatchList"
    }
    teams = paths["/api/tournaments/{tournament_id}/teams/"]
    assert (
        teams["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "type"
        ]
        == "array"
    )
    assert teams["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/TournamentTeam"
    }
    assert "201" in teams["post"]["responses"]

    for suffix, name, fields in (
        ("readiness/", "TournamentRefereeReady", {"expected_revision"}),
        (
            "tracker/events/latest/",
            "TournamentRefereeEventDelete",
            {"expected_revision", "event_id"},
        ),
    ):
        operation = paths[f"/api/tournaments/matches/{{match_id}}/{suffix}"]["delete"]
        body = operation["requestBody"]
        assert body["required"] is True
        assert body["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{name}"
        }
        assert set(schema["components"]["schemas"][name]["required"]) == fields
        assert "200" in operation["responses"]

    delete_team = paths["/api/tournaments/{tournament_id}/teams/{team_id}/"]["delete"]
    assert "requestBody" not in delete_team
    assert "content" not in delete_team["responses"]["204"]
    pdf = paths["/api/tournaments/{tournament_id}/referee-duties.pdf"]["get"]
    assert pdf["responses"]["200"]["content"]["application/pdf"]["schema"] == {
        "type": "string",
        "format": "binary",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/api/schema/", "/api/schema/swagger-ui/"])
def test_served_schema_is_staff_only(client: Client, path: str) -> None:
    """The contract maps every private endpoint, so it is not public."""
    assert client.get(path).status_code in {
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
    }
    staff = get_user_model().objects.create_user(username="schema-staff", is_staff=True)
    client.force_login(staff)
    assert client.get(path).status_code == HTTPStatus.OK
