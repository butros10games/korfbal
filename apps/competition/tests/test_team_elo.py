"""Cross-season club-team Elo: replay rules, persistence, rankings and predictions."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest
from rest_framework.test import APIClient

from apps.competition.domain.team_elo import (
    INITIAL_RATING,
    PHASE_CARRY,
    Fixture,
    expected_score,
    membership,
    outcome_probabilities,
    replay,
)
from apps.competition.models import Match, MatchRating, TeamRating
from apps.competition.services.match_prediction import match_prediction
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.team_elo import (
    INCREMENTAL_DAYS,
    refresh_team_ratings,
)
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_rating_preview import create_baseline
from apps.schedule.models import Season


START = datetime(2026, 9, 5, 12, tzinfo=UTC)
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
WINNER_SCORE = 10
EVEN = 0.5


@pytest.fixture
def no_overlap() -> Iterator[None]:
    """Make runs consecutive; production re-reads ten minutes of recent changes."""
    with patch("apps.competition.services.team_elo.OVERLAP", timedelta(0)):
        yield


@pytest.fixture
def predicted_match(season: Season) -> Match:
    """Publish one played youth result between two linked club teams."""
    create_baseline(season)
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    cache.clear()
    return Match.objects.select_related("local_match", "pool", "home_team__group").get()


def result(
    match: int, home: str, away: str, scores: tuple[int, int], **values: object
) -> Fixture:
    """Build a rated result in one poule, one day apart per match number."""
    return Fixture(
        match=match,
        phase=str(values.get("phase", "autumn")),
        pool=int(str(values.get("pool", 1))),
        starts_at=START + timedelta(days=match),
        home=home,
        away=away,
        home_score=scores[0],
        away_score=scores[1],
    )


def test_expected_score_and_outcomes_share_one_expectation() -> None:
    """Home advantage favours the host; W/D/L keeps the expected score."""
    even = expected_score(INITIAL_RATING, INITIAL_RATING)
    assert even > EVEN
    probabilities = outcome_probabilities(even)
    assert sum(probabilities.values()) == pytest.approx(1)
    assert probabilities["home"] + probabilities["draw"] / 2 == pytest.approx(even)
    assert all(value > 0 for value in probabilities.values())


def test_results_move_ratings_by_margin_and_conserve_points() -> None:
    """A decisive win moves more than a narrow one; transfers are zero-sum."""
    narrow = replay([result(1, "a", "b", (11, 10))])
    wide = replay([result(1, "a", "b", (20, 5))])
    assert wide.teams["a"].rating > narrow.teams["a"].rating > INITIAL_RATING
    for state in (narrow, wide):
        assert state.teams["a"].rating + state.teams["b"].rating == pytest.approx(
            2 * INITIAL_RATING
        )
    rated = wide.matches[0]
    assert (rated.home_rating, rated.home_games) == (INITIAL_RATING, 0)


def test_replay_ignores_input_order_and_unplayed_fixtures() -> None:
    """Chronological replay; fixtures without scores only define membership."""
    fixtures = [
        result(1, "a", "b", (12, 8)),
        result(2, "b", "c", (9, 9)),
        replace(result(3, "a", "c", (0, 0)), home_score=None, away_score=None),
    ]
    forward = replay(fixtures)
    assert replay(list(reversed(fixtures))) == forward
    assert [rated.match for rated in forward.matches] == [1, 2]


def test_a_new_phase_starts_near_the_new_poule() -> None:
    """Regraded poules carry information: 70% moves to the poule's average."""
    history = [result(index, "strong", "weak", (20, 5)) for index in range(1, 6)]
    before = replay(history)
    strong, weak = before.teams["strong"].rating, before.teams["weak"].rating
    indoor = {"phase": "indoor", "pool": 2}
    scheduled = replace(
        result(11, "weak", "other", (0, 0), **indoor), home_score=None, away_score=None
    )
    after = replay([
        *history,
        result(10, "strong", "newcomer", (10, 10), **indoor),
        scheduled,
    ])
    poule = (strong + weak) / 2
    entered = poule + PHASE_CARRY * (strong - poule)
    rated = after.matches[-1]
    assert rated.home_rating == pytest.approx(entered)
    # The newcomer joins at the poule's average once the strong team has entered.
    assert rated.away_rating == pytest.approx((entered + weak) / 2)
    assert after.teams["strong"].phase == "indoor"
    assert after.teams["weak"].phase != "indoor"


def test_resuming_from_stored_states_reproduces_a_full_replay() -> None:
    """Incremental refreshes rely on this: same matches, same final states."""
    fixtures = [
        result(1, "a", "b", (12, 8)),
        result(2, "c", "d", (9, 11)),
        result(3, "a", "c", (15, 4), phase="indoor", pool=2),
        result(4, "b", "d", (7, 7), phase="indoor", pool=2),
        result(5, "d", "a", (10, 13), phase="indoor", pool=2),
    ]
    full = replay(fixtures)
    prefix = replay(fixtures[:2])
    resumed = replay(fixtures[2:], initial=prefix.teams, members=membership(fixtures))
    assert resumed.matches == full.matches[2:]
    assert resumed.teams == full.teams


def test_teams_that_never_met_have_separate_comparison_groups() -> None:
    """Rankings across disconnected schedules would compare unrelated ratings."""
    state = replay([result(1, "a", "b", (10, 8)), result(2, "c", "d", (7, 9))])
    assert state.teams["a"].group == state.teams["b"].group
    assert state.teams["a"].group != state.teams["c"].group


def later_result(source: Match, days: float, scores: tuple[int, int]) -> Match:
    """Add another result between the same club teams, relative to now."""
    return Match.objects.create(
        season=source.season,
        external_id=f"later-{days}",
        pool=source.pool,
        home_team=source.away_team,
        away_team=source.home_team,
        starts_at=timezone.now() - timedelta(days=days),
        status="FINAL",
        home_score=scores[0],
        away_score=scores[1],
        result_observed_at=timezone.now(),
    )


def stored() -> tuple[list[tuple], list[tuple]]:
    """Every stored rating value, for exact comparisons."""
    return (
        list(MatchRating.objects.order_by("pk").values_list()),
        list(TeamRating.objects.order_by("pk").values_list()),
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("no_overlap")
def test_incremental_refresh_equals_a_full_replay(predicted_match: Match) -> None:
    """New, earlier-inserted and corrected results resume exactly where needed."""
    assert refresh_team_ratings()["status"] == "full"
    assert refresh_team_ratings()["status"] == "unchanged"
    later_result(predicted_match, 3, (14, 9))
    newer = later_result(predicted_match, 1, (8, 8))
    first = refresh_team_ratings()
    assert (first["status"], first["matches_created"]) == ("incremental", 2)
    # A result arriving late for an earlier kickoff replays the matches after it.
    later_result(predicted_match, 2, (5, 12))
    second = refresh_team_ratings()
    assert (second["matches_created"], second["matches_updated"]) == (1, 1)
    newer.home_score = 3
    newer.save()
    assert refresh_team_ratings()["matches_updated"] == 1
    incremental = stored()
    full = refresh_team_ratings(full=True)
    assert full["status"] == "full"
    assert (full["matches_updated"], full["teams_updated"]) == (0, 0)
    assert stored() == incremental


@pytest.mark.django_db
@pytest.mark.usefixtures("no_overlap")
def test_changed_ratings_are_upserted_without_case_updates(
    predicted_match: Match,
) -> None:
    """Rewriting history must stay linear: CASE-based bulk updates are quadratic."""
    later_result(predicted_match, 2, (14, 9))
    corrected = later_result(predicted_match, 1, (8, 8))
    refresh_team_ratings()
    corrected.home_score = 3
    corrected.save()
    with CaptureQueriesContext(connection) as queries:
        refreshed = refresh_team_ratings(full=True)
    assert refreshed["matches_updated"] == 1
    writes = [
        query["sql"] for query in queries if "competition_matchrating" in query["sql"]
    ]
    assert not any("CASE WHEN" in sql for sql in writes)
    assert any("ON CONFLICT" in sql for sql in writes)
    # Upserted values are stored exactly: replaying again changes nothing.
    again = refresh_team_ratings(full=True)
    assert (again["matches_updated"], again["teams_updated"]) == (0, 0)


@pytest.mark.django_db
@pytest.mark.usefixtures("no_overlap")
def test_old_changes_wait_for_the_nightly_replay(predicted_match: Match) -> None:
    """Changes more than 60 days back are deferred instead of replaying history."""
    refresh_team_ratings()
    later_result(predicted_match, INCREMENTAL_DAYS + 40, (10, 2))
    assert refresh_team_ratings() == {"status": "unchanged", "deferred": 1}
    assert refresh_team_ratings(full=True)["matches_created"] == 1


@pytest.mark.django_db
def test_a_held_refresh_lock_skips_the_run(predicted_match: Match) -> None:
    """Overlapping runs return immediately instead of writing concurrently."""

    @contextmanager
    def held() -> Iterator[bool]:
        yield False

    with patch("apps.competition.services.team_elo.refresh_lock", held):
        assert refresh_team_ratings() == {"status": "busy"}
    assert not MatchRating.objects.filter(match=predicted_match).exists()


@pytest.mark.django_db
def test_rankings_are_public_and_keep_rank_when_searching(
    predicted_match: Match,
) -> None:
    """Anonymous readers get ranks within the age group, also when filtering."""
    refresh_team_ratings()
    client = APIClient()
    assert client.get("/api/competition/rankings/").status_code == HTTP_BAD_REQUEST
    response = client.get("/api/competition/rankings/", {"age_group": "youth"})
    assert response.status_code == HTTP_OK
    rows = response.data["results"]
    assert [row["rank"] for row in rows] == [1, 2]
    assert rows[0]["team_name"] == "Example J1"
    assert rows[0]["phase_change"] > 0
    searched = client.get(
        "/api/competition/rankings/", {"age_group": "youth", "search": "other"}
    )
    assert [(row["rank"], row["team_name"]) for row in searched.data["results"]] == [
        (2, "Other J2")
    ]
    senior = client.get("/api/competition/rankings/", {"age_group": "senior"})
    assert senior.data["count"] == 0


@pytest.mark.django_db
def test_prediction_uses_ratings_known_at_kickoff(
    predicted_match: Match,
) -> None:
    """Played matches use stored pre-match ratings; upcoming ones current ratings."""
    native = predicted_match.local_match
    assert native is not None
    refresh_team_ratings()
    played = match_prediction(native)
    assert played["model"] == "elo-v2"
    assert played["home_rating"] == INITIAL_RATING
    assert played["provisional"] is True
    probabilities = played["outcome_calibration"]
    assert probabilities["home"] + probabilities["draw"] + probabilities["away"] == (
        pytest.approx(1)
    )
    MatchRating.objects.all().delete()
    upcoming = match_prediction(native)
    assert upcoming["home_rating"] > INITIAL_RATING
    assert upcoming["home_expected_result"] > played["home_expected_result"]
    TeamRating.objects.all().delete()
    assert match_prediction(native).get("model") != "elo-v2"
    assert predicted_match.home_score == WINNER_SCORE
