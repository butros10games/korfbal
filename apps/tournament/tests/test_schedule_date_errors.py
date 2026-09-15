"""API date boundaries and transaction rollback across tournament planners."""

from datetime import UTC, datetime
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client, override_settings
from django.utils import timezone
import pytest

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentPool,
    TournamentPoolEntry,
    TournamentStage,
    TournamentTeam,
)


pytestmark = pytest.mark.django_db


@pytest.fixture
def tournament(client: Client) -> Tournament:
    """Create an authenticated organizer with teams and one active field."""
    owner = get_user_model().objects.create_user(username="date-boundary-owner")
    client.force_login(owner)
    result = Tournament.objects.create(
        name="Boundary",
        slug="boundary",
        owner=owner,
        starts_at=timezone.now(),
        timezone="UTC",
    )
    TournamentField.objects.create(tournament=result, label="Field")
    for index in range(2):
        TournamentTeam.objects.create(tournament=result, name=f"Team {index}")
    return result


def _pool(tournament: Tournament) -> TournamentPool:
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Pool",
        kind=TournamentStage.Kind.POOL,
    )
    pool = TournamentPool.objects.create(tournament=tournament, stage=stage, name="A")
    for index, team in enumerate(tournament.teams.all()):
        TournamentPoolEntry.objects.create(pool=pool, team=team, seed_order=index)
    return pool


def _assert_error(
    response: HttpResponse, tournament: Tournament, revision: int
) -> None:
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "The schedule exceeds the supported date range."
    tournament.refresh_from_db()
    assert tournament.live_revision == revision


@pytest.mark.parametrize("day", ["9999-12-31", "0001-01-01"])
def test_import_rejects_single_row_date_overflow(
    client: Client,
    tournament: Tournament,
    day: str,
) -> None:
    """Reject both local end overflow and UTC underflow before import writes."""
    tournament.timezone = "Europe/Amsterdam"
    tournament.save(update_fields=["timezone"])
    revision = tournament.live_revision
    response = client.post(
        f"/api/tournaments/{tournament.pk}/schedule/import/",
        {
            "rows": [
                {
                    "date": day,
                    "start_time": "23:59" if day.startswith("9999") else "00:00",
                    "duration_minutes": 15,
                    "pool_name": "A",
                    "field_label": "Field",
                    "home_team_name": "New home",
                    "away_team_name": "New away",
                }
            ]
        },
        content_type="application/json",
    )
    _assert_error(response, tournament, revision)
    assert not tournament.matches.exists()
    assert not tournament.stages.exists()
    assert not tournament.teams.filter(name__startswith="New").exists()


def test_manual_match_rejects_date_overflow(
    client: Client, tournament: Tournament
) -> None:
    """Reject an unrepresentable match ending before a manual match is saved."""
    pool = _pool(tournament)
    teams = list(tournament.teams.all())
    revision = tournament.live_revision
    response = client.post(
        f"/api/tournaments/{tournament.pk}/matches/",
        {
            "pool_id": str(pool.pk),
            "field_id": str(tournament.fields.get().pk),
            "home_team_id": str(teams[0].pk),
            "away_team_id": str(teams[1].pk),
            "date": "9999-12-31",
            "start_time": "23:59",
            "duration_minutes": 15,
            "round_number": 1,
        },
        content_type="application/json",
    )
    _assert_error(response, tournament, revision)
    assert not tournament.matches.exists()


def test_cup_overflow_rolls_back_setup(client: Client, tournament: Tournament) -> None:
    """Roll back cup settings when draw construction exceeds the date range."""
    tournament.starts_at = datetime(9999, 12, 31, 23, 59, tzinfo=UTC)
    tournament.save(update_fields=["starts_at"])
    revision = tournament.live_revision
    original_rules = tournament.cup_rules
    original_duration = tournament.match_duration_minutes
    response = client.post(
        f"/api/tournaments/{tournament.pk}/cup/",
        {
            "expected_revision": revision,
            "generate_draw": True,
            "rules": {
                "regular_minutes": [10],
                "extra_minutes": [],
                "shootout_attempts": 1,
            },
        },
        content_type="application/json",
    )
    _assert_error(response, tournament, revision)
    assert tournament.cup_rules == original_rules
    assert tournament.match_duration_minutes == original_duration
    assert not tournament.matches.exists()
    assert not tournament.stages.exists()


@override_settings(TIME_ZONE="UTC")
def test_finals_overflow_rolls_back_created_matches(
    client: Client,
    tournament: Tournament,
) -> None:
    """Roll back finals rows already created before a later slot overflows."""
    pool = _pool(tournament)
    teams = list(tournament.teams.all())
    existing = TournamentMatch.objects.create(
        tournament=tournament,
        stage=pool.stage,
        pool=pool,
        home_team=teams[0],
        away_team=teams[1],
        field=tournament.fields.get(),
        starts_at=timezone.now(),
        match_number=1,
    )
    revision = tournament.live_revision
    response = client.post(
        f"/api/tournaments/{tournament.pk}/finals/generate/",
        {"qualifiers_per_pool": 2, "starts_at": "9999-12-31T23:59:00Z"},
        content_type="application/json",
    )
    _assert_error(response, tournament, revision)
    assert list(tournament.matches.values_list("pk", flat=True)) == [existing.pk]
    assert tournament.stages.count() == 1
