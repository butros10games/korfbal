"""Predictions use compatible pre-match baselines and observed result history."""

from datetime import timedelta

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.models import Allocation, Match, Pool, ResultRevision
from apps.competition.services import context_prediction as service
from apps.competition.services.match_prediction import match_prediction
from apps.competition.services.published_ratings import configure_ratings
from apps.competition.services.publishing import publish_catalogue
from apps.competition.tests.test_rating_preview import (
    PARAMETERS,
    START,
    create_baseline,
)
from apps.schedule.models import Season


@pytest.fixture
def predicted_match(season: Season) -> Match:
    """Publish synthetic native identities and select their allocation baselines."""
    baseline = create_baseline(season)
    publish_catalogue()
    configure_ratings(season, [baseline.pk], PARAMETERS, apply=True)
    return Match.objects.select_related("local_match").get()


@pytest.mark.django_db
def test_current_and_later_results_do_not_change_prematch_prediction(
    predicted_match: Match,
) -> None:
    """The target's own winner and later fixtures must never leak into its prior."""
    native = predicted_match.local_match
    assert native is not None
    before = match_prediction(native)
    assert before["status"] == "seeded"
    assert before["home_expected_result"] == pytest.approx(1 / 11)
    assert before["home_games"] == before["away_games"] == 0
    Match.objects.filter(pk=predicted_match.pk).update(home_score=99, away_score=0)
    later = Match.objects.get(pk=predicted_match.pk)
    later.pk = None
    later.external_id = "later"
    later.local_match = None
    later.starts_at += timedelta(days=1)
    later.save()
    assert match_prediction(native) == before
    response = APIClient().get(f"/api/matches/{native.pk}/summary/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["prediction"] == before


@pytest.mark.django_db
def test_known_revision_survives_later_correction_and_poll(
    predicted_match: Match,
) -> None:
    """Use the score known at kickoff rather than today's corrected score."""
    native = predicted_match.local_match
    assert native is not None
    before = match_prediction(native)
    earlier = Match.objects.get(pk=predicted_match.pk)
    earlier.pk = None
    earlier.external_id = "earlier"
    earlier.local_match = None
    earlier.starts_at = START + timedelta(hours=1)
    earlier.result_observed_at = native.start_time + timedelta(days=2)
    earlier.home_score, earlier.away_score = 0, 10
    earlier.save()
    assert match_prediction(native) == before  # no earlier observation is available
    ResultRevision.objects.create(
        match=earlier,
        observed_at=START + timedelta(hours=2),
        status="FINAL",
        home_score=10,
        away_score=0,
        automatic_result=False,
    )
    known = match_prediction(native)
    assert known["home_games"] == 1
    assert known["home_expected_result"] > before["home_expected_result"]
    ResultRevision.objects.create(
        match=earlier,
        observed_at=native.start_time + timedelta(days=1),
        status="FINAL",
        home_score=0,
        away_score=10,
        automatic_result=False,
    )
    assert match_prediction(native) == known


@pytest.mark.django_db
def test_simultaneous_and_awarded_matches_are_not_strength_evidence(
    predicted_match: Match,
) -> None:
    """Observed awards and simultaneous starts cannot update the kickoff prior."""
    native = predicted_match.local_match
    assert native is not None
    before = match_prediction(native)
    other = Match.objects.get(pk=predicted_match.pk)
    other.pk = None
    other.external_id = "simultaneous"
    other.local_match = None
    other.result_observed_at = START
    other.save()
    assert match_prediction(native) == before
    Match.objects.filter(pk=other.pk).update(starts_at=START, automatic_result=True)
    assert match_prediction(native) == before


@pytest.mark.django_db
def test_unmapped_and_reversed_identity_fall_back(predicted_match: Match) -> None:
    """Do not compare incompatible classes or use reversed native opponents."""
    native = predicted_match.local_match
    assert native is not None
    native.home_team_id, native.away_team_id = native.away_team_id, native.home_team_id
    assert match_prediction(native)["reason"] == "identity_conflict"
    Pool.objects.update(mapping_status="conflict")
    assert match_prediction(native)["status"] == "unavailable"


@pytest.mark.django_db
def test_missing_points_and_prebaseline_dates_are_neutral(
    predicted_match: Match,
) -> None:
    """A missing starting score must not become a guessed default rating."""
    native = predicted_match.local_match
    assert native is not None
    Allocation.objects.filter(team_name="Example J1").update(knkv_points=None)
    assert match_prediction(native)["reason"] == "missing_baseline"
    native.start_time = START - timedelta(seconds=1)
    assert match_prediction(native)["reason"] == "before_baseline"


@pytest.mark.django_db
@pytest.mark.parametrize("rating_slope", [0, 2])
def test_context_outcomes_are_exposed_by_the_existing_summary(
    predicted_match: Match,
    monkeypatch: pytest.MonkeyPatch,
    rating_slope: int,
) -> None:
    """Serve additive context probabilities without another client endpoint."""
    source = Match.objects.select_related(
        "pool__competition_class__edition", "season"
    ).get(pk=predicted_match.pk)
    assert source.pool is not None
    context = source.pool.competition_class
    assert context is not None
    key = (
        f"{context.edition.discipline}|{context.category}|{context.code}|"
        f"{context.age_group}|{context.colour}|{context.playing_format}"
    )
    monkeypatch.setattr(
        service,
        "model",
        lambda: {
            "version": "synthetic-context",
            "season": source.season.name,
            "available_from": START.isoformat(),
            "rating_scale": 20,
            "contexts": {
                key: {"home_bias": 0, "rating_slope": rating_slope, "draw_logit": 0}
            },
        },
    )
    native = predicted_match.local_match
    assert native is not None
    response = APIClient().get(f"/api/matches/{native.pk}/summary/")
    assert response.status_code == status.HTTP_200_OK
    calibration = response.data["prediction"]["outcome_calibration"]
    assert calibration["model"] == "synthetic-context"
    if rating_slope == 0:
        for outcome in ("home", "draw", "away"):
            assert calibration[outcome] == pytest.approx(1 / 3)
    else:
        assert calibration["home"] < calibration["away"]

    # A known earlier result updates Elo, but must not change the preseason
    # feature values used by the independently fitted context model.
    before = response.data["prediction"]
    earlier = Match.objects.get(pk=predicted_match.pk)
    earlier.pk = None
    earlier.external_id = "earlier-calibration"
    earlier.local_match = None
    earlier.starts_at = START + timedelta(hours=1)
    earlier.result_observed_at = START + timedelta(hours=2)
    earlier.home_score, earlier.away_score = 10, 0
    earlier.save()
    response = APIClient().get(f"/api/matches/{native.pk}/summary/")
    assert response.status_code == status.HTTP_200_OK
    after = response.data["prediction"]
    assert after["home_games"] == after["away_games"] == 1
    assert after["home_expected_result"] > before["home_expected_result"]
    assert after["outcome_calibration"] == calibration
