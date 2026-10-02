"""Cross-season club-team Elo: replay rules, persistence, rankings and predictions."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from django.core.cache import cache
import pytest
from rest_framework.test import APIClient

from apps.competition.domain.team_elo import (
    INITIAL_RATING,
    PHASE_CARRY,
    Fixture,
    expected_score,
    outcome_probabilities,
    replay,
)
from apps.competition.models import Match, MatchRating, TeamRating
from apps.competition.services.match_prediction import match_prediction
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.team_elo import refresh_team_ratings
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_rating_preview import create_baseline
from apps.schedule.models import Season


START = datetime(2026, 9, 5, 12, tzinfo=UTC)
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
WINNER_SCORE = 10
EVEN = 0.5


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


def test_teams_that_never_met_have_separate_comparison_groups() -> None:
    """Rankings across disconnected schedules would compare unrelated ratings."""
    state = replay([result(1, "a", "b", (10, 8)), result(2, "c", "d", (7, 9))])
    assert state.teams["a"].group == state.teams["b"].group
    assert state.teams["a"].group != state.teams["c"].group


@pytest.mark.django_db
def test_refresh_stores_ratings_and_skips_unchanged_history(
    predicted_match: Match,
) -> None:
    """The first refresh writes every row; an unchanged history writes nothing."""
    first = refresh_team_ratings()
    assert first["status"] == "refreshed"
    assert first["matches_created"] == 1
    assert refresh_team_ratings()["status"] == "unchanged"
    forced = refresh_team_ratings(force=True)
    assert (forced["matches_updated"], forced["teams_updated"]) == (0, 0)
    stored = MatchRating.objects.get(match=predicted_match)
    assert stored.home_rating == INITIAL_RATING
    winner = TeamRating.objects.get(team=predicted_match.home_team.group.local_team)
    assert winner.rating > INITIAL_RATING
    assert winner.competition_class_id == predicted_match.pool.competition_class_id
    Match.objects.filter(pk=predicted_match.pk).update(home_score=1, away_score=9)
    corrected = refresh_team_ratings()
    assert corrected["matches_updated"] == 1
    winner.refresh_from_db()
    assert winner.rating < INITIAL_RATING


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
