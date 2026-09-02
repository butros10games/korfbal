"""Existing tournament schedule import tests."""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
import pytest

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentPool,
    TournamentStage,
    TournamentTeam,
)


pytestmark = pytest.mark.django_db


def _managed_tournament(client: Client) -> Tournament:
    owner = get_user_model().objects.create_user(username="import-manager")
    client.force_login(owner)
    return Tournament.objects.create(
        name="Bestaand toernooi",
        slug="bestaand-toernooi",
        owner=owner,
        starts_at=timezone.now(),
        timezone="Europe/Amsterdam",
    )


def test_import_creates_missing_entities_and_preserves_supplied_plan(
    client: Client,
) -> None:
    """A pasted schedule becomes normal tournament pools, teams, fields and matches."""
    tournament = _managed_tournament(client)
    existing_team = TournamentTeam.objects.create(
        tournament=tournament,
        name="Fortuna 1",
    )
    rows = [
        {
            "date": "2027-06-12",
            "start_time": "09:00",
            "pool_name": "Poule A",
            "field_label": "Veld 1",
            "home_team_name": "fortuna 1",
            "away_team_name": "PKC 1",
        },
        {
            "date": "2027-06-12",
            "start_time": "09:25",
            "pool_name": "Poule A",
            "field_label": "Veld 1",
            "home_team_name": "Fortuna 1",
            "away_team_name": "KZ 1",
        },
        {
            "date": "2027-06-12",
            "start_time": "09:50",
            "pool_name": "Poule A",
            "field_label": "Veld 1",
            "home_team_name": "PKC 1",
            "away_team_name": "KZ 1",
        },
    ]

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/schedule/import/",
        data={"rows": rows},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    expected_team_names = {"Fortuna 1", "PKC 1", "KZ 1"}
    assert tournament.teams.count() == len(expected_team_names)
    assert tournament.fields.count() == 1
    assert tournament.pools.count() == 1
    assert tournament.matches.count() == len(rows)
    assert TournamentTeam.objects.get(pk=existing_team.pk).name == "Fortuna 1"
    payload = response.json()
    assert [row["team_name"] for row in payload["pools"][0]["standings"]] == [
        "Fortuna 1",
        "PKC 1",
        "KZ 1",
    ]
    first_match = tournament.matches.order_by("match_number").first()
    assert first_match is not None
    assert first_match.starts_at == datetime(2027, 6, 12, 7, 0, tzinfo=UTC)
    assert first_match.home_team_id == existing_team.pk


def test_import_rejects_overlaps_without_replacing_existing_schedule(
    client: Client,
) -> None:
    """Invalid pasted rows fail atomically and retain the current plan."""
    tournament = _managed_tournament(client)
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Bestaand",
        kind=TournamentStage.Kind.POOL,
    )
    pool = TournamentPool.objects.create(
        tournament=tournament,
        stage=stage,
        name="Poule A",
    )
    field = TournamentField.objects.create(tournament=tournament, label="Veld oud")
    teams = [
        TournamentTeam.objects.create(tournament=tournament, name=f"Oud {index}")
        for index in range(1, 3)
    ]
    existing_match = TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        pool=pool,
        field=field,
        home_team=teams[0],
        away_team=teams[1],
    )
    rows = [
        {
            "date": "2027-06-12",
            "start_time": "09:00",
            "pool_name": "Poule A",
            "field_label": "Veld 1",
            "home_team_name": "Team A",
            "away_team_name": "Team B",
        },
        {
            "date": "2027-06-12",
            "start_time": "09:10",
            "pool_name": "Poule A",
            "field_label": "Veld 1",
            "home_team_name": "Team C",
            "away_team_name": "Team D",
        },
    ]

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/schedule/import/",
        data={"rows": rows},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "overlap" in response.json()["detail"]
    assert tournament.matches.get() == existing_match
    assert tournament.teams.count() == len(teams)
