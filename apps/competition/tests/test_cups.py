"""Observed cup metadata, zero-minute shootouts and native adoption contracts."""

from copy import deepcopy
from datetime import timedelta
from http import HTTPStatus
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.club.models import Club as NativeClub
from apps.competition.models import CupCompetition, CupFixture, Match, Team
from apps.competition.services.cup_publication import publish_cup
from apps.competition.services.cups import import_cup_fixture
from apps.competition.services.importer import Importer
from apps.competition.services.match_timing import import_timing_details
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season
from apps.team.models import Team as NativeTeam
from apps.tournament.models import Tournament, TournamentField


pytestmark = pytest.mark.django_db
REGULAR_MINUTES = 60
CUP_PERIODS = [
    {"Description": "1e helft", "PlayTime": 30},
    {"Description": "2e helft", "PlayTime": 30},
    {"Description": "1e verlenging", "PlayTime": 5},
    {"Description": "2e verlenging", "PlayTime": 5},
    {"Description": "Strafworpserie", "PlayTime": 0},
]


def cup_match(season: Season) -> Match:
    """Import one synthetic provider fixture with an observed cup class label."""
    row = match_payload()
    row["Pool"]["ClassName"] = "Damesbeker"
    Importer(season, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    return Match.objects.get()


@pytest.mark.parametrize("resolution", ["MINUTE", "NONE"])
def test_cup_timing_retains_possible_periods_without_inflating_duration(
    season: Season, resolution: str
) -> None:
    """Optional extra time and untimed penalties do not imply seventy minutes played."""
    match = cup_match(season)
    updated = match.updated_at
    observed = timezone.now()
    payload = {
        "PublicMatchId": match.external_id,
        "Duration": 60,
        "EventTimeResolution": resolution,
        "MatchPeriod": deepcopy(CUP_PERIODS),
    }
    import_timing_details(season, match.external_id, payload, observed)
    import_timing_details(
        season, match.external_id, payload, observed + timedelta(seconds=1)
    )
    match.refresh_from_db()
    assert match.playing_time_minutes == REGULAR_MINUTES
    assert match.match_periods == CUP_PERIODS
    assert match.updated_at == updated
    assert CupCompetition.objects.count() == 1
    fixture = CupFixture.objects.get()
    assert fixture.round_number is None
    assert not fixture.round_name
    assert fixture.next_fixture_id is None
    assert fixture.local_match_id is None


@pytest.mark.parametrize(
    "period",
    [
        {"Description": "1e helft", "PlayTime": 0},
        {"Description": "Strafworpserie", "PlayTime": 0},
        {"Description": "Strafworpserie", "PlayTime": -1},
        {"Description": "Strafworpserie", "PlayTime": False},
        {"Description": None, "PlayTime": 0},
    ],
)
def test_unknown_zero_or_invalid_periods_still_reject(
    season: Season, period: dict
) -> None:
    """The observed shootout exception does not admit arbitrary invalid timing."""
    match = cup_match(season)
    with pytest.raises(ValueError, match="duration/periods"):
        import_timing_details(
            season,
            match.external_id,
            {
                "PublicMatchId": match.external_id,
                "Duration": 60,
                "EventTimeResolution": "MINUTE",
                "MatchPeriod": [period],
            },
            timezone.now(),
        )
    match.refresh_from_db()
    assert match.playing_time_observed_at is None


def assert_adopted_results(cup: CupCompetition) -> None:
    """Native results preserve provider scores without inventing shootout winners."""
    for imported in CupFixture.objects.filter(competition=cup).select_related(
        "match", "local_match"
    ):
        local = imported.local_match
        provider = imported.match
        assert local.created_at
        assert local.updated_at
        assert local.starts_at == provider.starts_at
        assert local.revision == 0
        if provider.status == "FINAL":
            assert (local.home_score, local.away_score) == (
                provider.home_score,
                provider.away_score,
            )
            assert local.cup_state["phase"] == "unobserved"
            if provider.home_score == provider.away_score:
                assert local.winner_id is None
            else:
                assert local.winner_id == (
                    local.home_team_id
                    if provider.home_score > provider.away_score
                    else local.away_team_id
                )
        else:
            assert local.home_score is None
            assert local.away_score is None
            assert local.winner_id is None


@pytest.mark.parametrize("extra_fixtures", [0, 126])
def test_adoption_preserves_unknown_round_and_provider_result(
    season: Season,
    extra_fixtures: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ownership creates fixtures without guessing a bracket."""
    source = cup_match(season)
    native_team = NativeTeam.objects.create(
        club=NativeClub.objects.create(name="Cup native club"), name="1"
    )
    source.home_team.group.local_team = native_team
    source.home_team.group.save(update_fields=["local_team"])
    variant = Team.objects.create(
        season=season,
        external_id="cup-variant",
        club=source.home_team.club,
        group=source.home_team.group,
        name=source.home_team.name,
        sport=source.home_team.sport,
    )
    second = Match.objects.create(
        season=season,
        external_id="second-cup-fixture",
        pool=source.pool,
        home_team=variant,
        away_team=source.away_team,
        starts_at=source.starts_at + timedelta(days=1),
        status="SCHEDULED",
    )
    import_cup_fixture(second)
    for index in range(extra_fixtures):
        extra = Match.objects.create(
            season=season,
            external_id=f"extra-cup-{index}",
            pool=source.pool,
            home_team=variant,
            away_team=source.away_team,
            starts_at=source.starts_at + timedelta(days=index + 2),
            status="FINAL",
            home_score=8,
            away_score=8 if index % 2 else 10,
        )
        import_cup_fixture(extra)
    cup = CupCompetition.objects.get()
    owner = get_user_model().objects.create_user(username="cup-manager")
    tournament = Tournament.objects.create(
        name="Adopted Cup",
        slug="adopted-cup",
        owner=owner,
        starts_at=source.starts_at,
        cup_rules={
            "regular_minutes": [30, 30],
            "extra_minutes": [5, 5],
            "shootout_attempts": 3,
        },
    )
    # A late failure after inserting native rows must roll back the whole adoption.
    with monkeypatch.context() as patch:
        patch.setattr(
            CupFixture.objects,
            "bulk_update",
            Mock(side_effect=IntegrityError("fixture link failed")),
        )
        with pytest.raises(IntegrityError, match="fixture link failed"):
            publish_cup(cup.pk, str(tournament.pk))
    assert not tournament.teams.exists()
    assert not tournament.stages.exists()
    assert not tournament.matches.exists()
    assert not CupFixture.objects.filter(local_match__isnull=False).exists()
    cup.refresh_from_db()
    assert cup.local_tournament_id is None
    fixture_count = CupFixture.objects.count()
    with CaptureQueriesContext(connection) as queries:
        assert publish_cup(cup.pk, str(tournament.pk)) == fixture_count
    maximum_queries = 24
    assert len(queries) <= maximum_queries
    assert_adopted_results(cup)
    # Link the known ID without fetching one native Team per source group.
    native_reads = [
        query
        for query in queries
        if query["sql"].lstrip().startswith("SELECT")
        and f'FROM "{NativeTeam._meta.db_table}"' in query["sql"]
    ]
    assert not native_reads
    assert tournament.teams.get(linked_team=native_team).name == source.home_team.name
    assert publish_cup(cup.pk, str(tournament.pk)) == 0
    assert tournament.teams.count() == len({
        source.home_team.group_id,
        source.away_team.group_id,
    })
    fixture = CupFixture.objects.get(match=source)
    assert fixture.local_match.round_number is None
    assert fixture.local_match.next_match_id is None
    fixture.local_match.home_score = 99
    fixture.local_match.save()
    original_score = source.home_score
    source.refresh_from_db()
    assert source.home_score == original_score


def test_adopted_fixture_can_feed_an_imported_destination(
    season: Season, client: Client
) -> None:
    """Planning a native winner slot never changes the official source entrants."""
    source = cup_match(season)
    source.status = "SCHEDULED"
    source.save(update_fields=["status"])
    opponent = Team.objects.create(
        season=season,
        external_id="cup-final-opponent",
        name="Other finalist",
        club=source.home_team.club,
    )
    final = Match.objects.create(
        season=season,
        external_id="cup-final",
        pool=source.pool,
        home_team=source.home_team,
        away_team=opponent,
        starts_at=source.starts_at + timedelta(days=1),
        status="SCHEDULED",
    )
    import_cup_fixture(final)
    owner = get_user_model().objects.create_user(username="import-planner")
    tournament = Tournament.objects.create(
        name="Imported Cup",
        slug="imported-cup",
        owner=owner,
        starts_at=source.starts_at,
        cup_rules={
            "regular_minutes": [30, 30],
            "extra_minutes": [],
            "shootout_attempts": 3,
        },
    )
    publish_cup(CupCompetition.objects.get().pk, str(tournament.pk))
    field = TournamentField.objects.create(tournament=tournament, label="Field 1")
    match = CupFixture.objects.get(match=source).local_match
    destination = CupFixture.objects.get(match=final).local_match
    client.force_login(owner)
    response = client.post(
        f"/api/tournaments/matches/{match.pk}/cup/plan/",
        {
            "expected_revision": match.revision,
            "round_name": "Halve finale",
            "round_number": 1,
            "starts_at": source.starts_at.isoformat(),
            "field_id": str(field.pk),
            "next_match_id": str(destination.pk),
            "winner_to_side": "home",
            "replace_destination_team": True,
            "expected_destination_revision": destination.revision,
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK, response.content
    destination.refresh_from_db()
    final.refresh_from_db()
    assert destination.home_team_id is None
    assert final.home_team_id == source.home_team_id
    assert final.status == "SCHEDULED"
