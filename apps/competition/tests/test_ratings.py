"""Pure Elo behavior, corrected-result rebuilding and authenticated read APIs."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.domain.elo import INITIAL_RATING, RatedResult, calculate
from apps.competition.models import Match, Team
from apps.competition.services.importer import Importer
from apps.competition.services.ratings import team_ratings
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


MATCH_TIME = datetime(2026, 9, 5, 12, tzinfo=UTC)


def test_equal_team_win_is_zero_sum() -> None:
    """One win moves equal teams equally in opposite directions."""
    ratings = calculate(
        {1: "outdoor", 2: "outdoor"}, [RatedResult("M1", MATCH_TIME, 1, 2, 10, 0)]
    )
    assert ratings[1].value > INITIAL_RATING > ratings[2].value
    assert ratings[1].value + ratings[2].value == INITIAL_RATING * 2
    assert ratings[1].games == ratings[2].games == 1
    assert ratings[1].comparison_group == ratings[2].comparison_group


def test_draw_and_disconnected_groups() -> None:
    """A draw preserves equal ratings; disconnected teams stay separate."""
    ratings = calculate(
        {1: "outdoor", 2: "outdoor", 3: "outdoor", 4: "indoor"},
        [RatedResult("M1", MATCH_TIME, 1, 2, 0, 0)],
    )
    assert all(rating.value == INITIAL_RATING for rating in ratings.values())
    assert ratings[3].comparison_group != ratings[1].comparison_group
    assert ratings[4].games == 0


def test_simultaneous_results_do_not_depend_on_provider_order() -> None:
    """Same-time games use a frozen pre-batch baseline."""
    first = RatedResult("M1", MATCH_TIME, 1, 2, 10, 5)
    second = RatedResult("M2", MATCH_TIME, 1, 3, 3, 8)
    sports = {1: "outdoor", 2: "outdoor", 3: "outdoor"}
    ratings = calculate(sports, [first, second])
    assert ratings == calculate(sports, [second, first])
    assert ratings[1].value == INITIAL_RATING


def test_cross_sport_results_do_not_change_ratings() -> None:
    """Never establish false comparison evidence between indoor and outdoor teams."""
    ratings = calculate(
        {1: "indoor", 2: "outdoor"}, [RatedResult("M1", MATCH_TIME, 1, 2, 10, 0)]
    )
    assert all(rating.games == 0 for rating in ratings.values())


@pytest.fixture
def season() -> Season:
    """Provide an explicit season for the read model."""
    return Season.objects.create(
        name="2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )


@pytest.mark.django_db
def test_corrected_result_invalidates_cached_rating(season: Season) -> None:
    """A result correction reverses the winner without applying another game."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "CT1", {"MatchResult": [row]})
    first = {team["external_id"]: team for team in team_ratings(season.pk)["results"]}
    assert first["T1"]["rating"] < INITIAL_RATING
    row["HomeResult"]["Score"] = 20
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    corrected = {
        team["external_id"]: team for team in team_ratings(season.pk)["results"]
    }
    assert corrected["T1"]["rating"] > INITIAL_RATING
    assert corrected["T1"]["games"] == 1
    assert corrected["T1"]["provisional"]


@pytest.mark.django_db
@pytest.mark.parametrize("excluded", ["SCHEDULED", "CANCELLED", "AWARDED"])
def test_nonplayed_results_are_excluded(season: Season, excluded: str) -> None:
    """Scheduled, cancelled and automatic results are not playing-strength evidence."""
    row = match_payload()
    row["Status"] = "FINAL" if excluded == "AWARDED" else excluded
    row["AutoResult"] = "AWARDED" if excluded == "AWARDED" else None
    Importer(season, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    assert all(team["games"] == 0 for team in team_ratings(season.pk)["results"])


@pytest.mark.django_db
def test_ratings_api_requires_season_and_paginates(season: Season) -> None:
    """Expose bounded, authenticated ratings without inventing a global rank."""
    client = APIClient()
    assert client.get("/api/competition/ratings/").status_code in {401, 403}
    client.force_authenticate(get_user_model().objects.create_user(username="ratings"))
    assert (
        client.get("/api/competition/ratings/").status_code
        == status.HTTP_400_BAD_REQUEST
    )
    Importer(season, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    response = client.get(f"/api/competition/ratings/?season={season.pk}&page_size=1")
    assert response.status_code == status.HTTP_200_OK
    assert len(response.data["results"]) == 1
    assert response.data["next"]
    assert response.data["model"] == "elo-v1"
    assert response.data["results"][0]["provisional"]


@pytest.mark.django_db
def test_sport_change_invalidates_cached_population(season: Season) -> None:
    """An updated source sport cannot keep an old comparison group in cache."""
    Importer(season, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [match_payload()]}
    )
    team_ratings(season.pk)
    Team.objects.filter(external_id="T1").update(sport="indoor")
    assert all(team["games"] == 0 for team in team_ratings(season.pk)["results"])
    assert Match.objects.count() == 1


@pytest.mark.django_db
def test_unchanged_result_poll_preserves_cached_ratings(season: Season) -> None:
    """Repeated upstream observations do not rebuild the full season Elo table."""
    now = timezone.now()
    payload = {"MatchResult": [match_payload()]}
    Importer(season, now).apply("club_results", "CT1", payload)
    initial = team_ratings(season.pk)
    Importer(season, now + timedelta(seconds=1)).apply("club_results", "CT1", payload)
    assert team_ratings(season.pk) == initial
