"""Forecast contracts: historical knowledge, posterior uncertainty and safe serving."""

from copy import deepcopy
from datetime import timedelta
import json
import math
from pathlib import Path
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import override_settings
import pytest
from rest_framework.test import APIClient

from apps.competition.domain.score_forecast import (
    DRAW_COUNT,
    context_key,
    poisson,
    rate_draws,
    summarize,
)
from apps.competition.models import Match, RatingConfiguration, ResultRevision
from apps.competition.offline.score_training import fit, snapshot
from apps.competition.offline.score_validation import (
    backtest,
    forward_audit,
    metric_summary,
)
from apps.competition.queries.forecast_export import export_rows, features
from apps.competition.queries.forecast_legacy import predictions
from apps.competition.services import score_prediction as serving
from apps.competition.services.match_prediction import known_results, rating_prediction
from apps.competition.services.published_ratings import configure_ratings
from apps.competition.services.publishing import publish_catalogue
from apps.competition.tests.test_rating_preview import (
    PARAMETERS,
    START,
    create_baseline,
)
from apps.schedule.models import (
    Match as NativeMatch,
    Season,
)


TARGET_THRESHOLD = 0.75
EXPECTED_COMPARISONS = 4
EXPECTED_POOLS = 3
HTTP_OK = 200


@pytest.fixture
def predicted_match(season: Season) -> Match:
    """Publish synthetic identities for the summary integration contract."""
    baseline = create_baseline(season)
    publish_catalogue()
    configure_ratings(season, [baseline.pk], PARAMETERS, apply=True)
    return Match.objects.select_related("local_match").get()


@pytest.fixture
def history() -> list[dict]:
    """Synthetic results with explicit pre-kickoff metadata and delayed labels."""
    return [
        {
            "match": str(index),
            "season": "synthetic-season",
            "discipline": "outdoor",
            "phase": "autumn",
            "gender": "mixed",
            "category": "b",
            "age_group": "senior",
            "colour": "unknown",
            "playing_format": "eight",
            "team_kind": "senior",
            "class": "class-a",
            "class_code": "B",
            "pool": f"p{index % 3}",
            "pool_label": "Synthetic",
            "home": f"home-{index % 3}",
            "away": f"away-{index % 3}",
            "duration": 60,
            "duration_observed_at": START.isoformat(),
            "starts_at": (START + timedelta(days=index + 1)).isoformat(),
            "revisions": [
                {
                    "revision": 1,
                    "observed_at": (
                        START + timedelta(days=index + 1, hours=2)
                    ).isoformat(),
                    "status": "FINAL",
                    "automatic_result": False,
                    "home_score": 10 + index % 3,
                    "away_score": 5 + index % 2,
                }
            ],
        }
        for index in range(24)
    ]


def test_revision_replay_does_not_leak_late_scores_or_metadata(
    history: list[dict],
) -> None:
    """New scores and awards replace old revisions only after observation."""
    cutoff = START + timedelta(days=16)
    before = snapshot(history, cutoff)
    history[0]["revisions"].append({
        **history[0]["revisions"][0],
        "revision": 2,
        "home_score": 99,
        "observed_at": (cutoff + timedelta(days=1)).isoformat(),
    })
    assert snapshot(history, cutoff) == before
    assert (
        snapshot(history, cutoff + timedelta(days=2))[0]["home_score"]
        == history[0]["revisions"][-1]["home_score"]
    )
    history[0]["revisions"][-1]["automatic_result"] = True
    assert all(
        row["match"] != "0" for row in snapshot(history, cutoff + timedelta(days=2))
    )
    history[1]["duration_observed_at"] = cutoff.isoformat()
    assert all(row["match"] != "1" for row in snapshot(history, cutoff))


def test_fit_is_reproducible_and_held_out_labels_cannot_change_it(
    history: list[dict],
) -> None:
    """Chronological fitting excludes later matches even if their labels are present."""
    cutoff = START + timedelta(days=16)
    artifact = fit(history, cutoff)
    for row in history[16:]:
        row["revisions"][0]["home_score"] = 99
    assert fit(history, cutoff) == artifact
    assert artifact["approved"] is False
    draws = rate_draws(artifact, history[0])
    assert draws is not None
    assert len(draws) == DRAW_COUNT
    assert len({home for home, _ in draws}) > 1
    assert sum(home for home, _ in draws) > sum(away for _, away in draws)
    assert rate_draws(artifact, {**history[0], "playing_format": "four"}) is None
    assert rate_draws(artifact, {**history[0], "season": "next-year"}) is None


def test_unseen_teams_and_poules_integrate_their_prior(history: list[dict]) -> None:
    """Cold starts retain uncertainty instead of treating unknown effects as zero."""
    artifact = fit(history, START + timedelta(days=25))
    row = {**history[0], "home": "new-home", "away": "new-away", "pool": "new-pool"}
    draws = rate_draws(artifact, row)
    assert draws is not None
    assert draws == rate_draws(artifact, row)
    assert len({home for home, _ in draws}) == DRAW_COUNT
    shorter = rate_draws(artifact, {**row, "duration": 40})
    assert shorter is not None
    for full, short in zip(draws, shorter, strict=True):
        assert short == pytest.approx([value * 2 / 3 for value in full])


def test_joint_distribution_and_quantile_targets() -> None:
    """Target satisfies its stated marginal threshold; mixtures preserve uncertainty."""
    result = summarize([[8.4, 6.9]])
    assert sum(result["probabilities"].values()) == pytest.approx(1)
    assert result["most_likely_score"] == {"home": 8, "away": 6}
    target = result["opponent_target_75"]["home"]
    opponent = poisson(6.9)
    assert sum(opponent[:target]) >= TARGET_THRESHOLD
    assert sum(opponent[: target - 1]) < TARGET_THRESHOLD
    mixture = summarize([[2, 2], [18, 18]])
    fixed = summarize([[10, 10]])
    assert mixture["expected_goals"] == fixed["expected_goals"]
    assert mixture["interval_80"]["home"][1] > fixed["interval_80"]["home"][1]
    assert mixture["probabilities"]["draw"] != pytest.approx(
        fixed["probabilities"]["draw"]
    )


def test_backtest_reports_coverage_and_refuses_insufficient_promotion(
    history: list[dict],
) -> None:
    """Report genuinely held-out metrics; small validation samples cannot activate."""
    report = backtest(
        history,
        [START + timedelta(days=16), START + timedelta(days=20)],
        START + timedelta(days=25),
    )
    assert report["passed"] is False
    assert report["metrics"]["candidate"]["matches"] == len(history) - 15
    assert math.isfinite(report["metrics"]["candidate"]["score_log_loss"])
    assert "calibration" in report["metrics"]["candidate"]
    assert len(report["paired_difference_95"]) == EXPECTED_COMPARISONS


def test_forward_audit_uses_exact_artifact_availability_window(
    history: list[dict], tmp_path: Path
) -> None:
    """Forward evidence freezes training and excludes pre-availability fixtures."""
    cutoff = START + timedelta(days=16)
    through = START + timedelta(days=25)
    artifact = fit(history, cutoff)
    artifact.update(
        approved=True,
        available_from=(cutoff + timedelta(days=1)).isoformat(),
        input_sha256="synthetic",
        validation={"passed": True},
    )
    report = forward_audit(history, artifact, through)
    assert report["mode"] == "deployed-forward-audit"
    assert report["eligible_matches"] == len(history) - 16
    assert report["evaluated_matches"] == len(history) - 16
    assert report["evaluated_pools"] == EXPECTED_POOLS
    assert report["metrics"]["candidate"]["matches"] == len(history) - 16
    assert len(report["paired_difference_95"]) == EXPECTED_COMPARISONS

    source = tmp_path / "source.json"
    model = tmp_path / "artifact.json"
    destination = tmp_path / "audit.json"
    source.write_text(
        json.dumps({
            "schema": 1,
            "rows": history,
            "exported_at": through.isoformat(),
        })
    )
    model.write_text(json.dumps(artifact))
    call_command(
        "audit_score_forecasts",
        input=source,
        artifact=model,
        report=destination,
        through=through,
    )
    assert json.loads(destination.read_text())["artifact"]["input_sha256"] == (
        "synthetic"
    )


@pytest.mark.parametrize("weights", [None, {"home": 1.3, "draw": 0.6, "away": 0.8}])
@pytest.mark.parametrize(
    "draws", [[[8.4, 6.9]], [[2, 2], [18, 18]], [[0.01, 35], [40, 0.2]]]
)
def test_vectorized_metrics_preserve_serving_distribution(
    draws: list, weights: dict | None
) -> None:
    """Speedups retain conditional probabilities, marginal intervals and means."""
    original = summarize(draws, weights)
    actual = metric_summary(draws, weights)
    assert actual["probabilities"] == pytest.approx(
        original["probabilities"], abs=1e-12
    )
    assert actual["expected_goals"] == pytest.approx(
        original["expected_goals"], abs=1e-12
    )
    assert actual["interval_80"] == original["interval_80"]


@pytest.mark.django_db
def test_batch_legacy_matches_existing_predictor(predicted_match: Match) -> None:
    """Batched replay retains the published predictor's result and provenance."""
    native = predicted_match.local_match
    assert native is not None
    through = START + timedelta(days=30)
    result = predictions([str(predicted_match.pk)], through, lambda message: None)
    assert result[str(predicted_match.pk)] == rating_prediction(native)


@pytest.mark.django_db
def test_batch_legacy_reuses_replay_for_simultaneous_fixtures(
    predicted_match: Match,
) -> None:
    """Shared class/cutoff calculations remain identical for distinct match IDs."""
    native = predicted_match.local_match
    assert native is not None
    duplicate_native = NativeMatch.objects.create(
        home_team=native.home_team,
        away_team=native.away_team,
        season=native.season,
        start_time=native.start_time,
    )
    duplicate = deepcopy(predicted_match)
    duplicate.pk = None
    duplicate.external_id = "second-simultaneous-match"
    duplicate.local_match = duplicate_native
    duplicate.save()
    expected = {
        str(predicted_match.pk): rating_prediction(native),
        str(duplicate.pk): rating_prediction(duplicate_native),
    }
    with patch(
        "apps.competition.queries.forecast_legacy.known_results", wraps=known_results
    ) as replay:
        actual = predictions(
            list(expected), START + timedelta(days=30), lambda message: None
        )
    assert actual == expected
    assert all(value["status"] == "seeded" for value in actual.values())
    assert replay.call_count == 1


def test_refresh_preserves_audit_before_rejected_fit_and_refuses_overwrite(
    history: list[dict], tmp_path: Path
) -> None:
    """A failed gate leaves evidence and cannot overwrite an earlier run."""
    season_id = "00000000-0000-0000-0000-000000000001"
    for row in history:
        row["season"] = season_id
    cutoff = START + timedelta(days=16)
    through = START + timedelta(days=19)
    artifact = fit(history, cutoff)
    artifact.update(
        approved=True,
        validation={
            "passed": True,
            "folds": [{"cutoff": (START + timedelta(days=12)).isoformat()}],
        },
    )
    model = tmp_path / "served.json"
    model.write_text(json.dumps(artifact))
    root = tmp_path / "run"

    def invoke(command: str, *arguments: str, **options: object) -> None:
        if command == "export_score_forecasts":
            Path(str(options["output"])).write_text(
                json.dumps({
                    "schema": 1,
                    "rows": history,
                    "exported_at": through.isoformat(),
                    "metadata_history": "synthetic",
                }),
                encoding="utf-8",
            )
        else:
            if command == "fit_score_forecasts":
                assert (root / "forward-audit.json").is_file()
                assert (root / "head-to-head.json").is_file()
            call_command(command, *arguments, **options)

    with patch(
        "apps.competition.management.commands.refresh_score_forecasts.call_command",
        side_effect=invoke,
    ):
        call_command(
            "refresh_score_forecasts",
            season=season_id,
            artifact=model,
            output_dir=root,
            through=through,
            challenger=model,
        )
        before = (root / "forward-audit.json").read_bytes()
        assert json.loads((root / "status.json").read_text())["status"] == "rejected"
        assert json.loads((root / "candidate.json").read_text())["approved"] is False
        with pytest.raises(CommandError, match="File exists"):
            call_command(
                "refresh_score_forecasts",
                season=season_id,
                artifact=model,
                output_dir=root,
                through=through,
            )
        assert (root / "forward-audit.json").read_bytes() == before


def test_artifact_boundary_is_database_free_and_fails_closed(
    history: list[dict], tmp_path: Path
) -> None:
    """Serving needs only the frozen file, exact context, and known match metadata."""
    cutoff = START + timedelta(days=25)
    artifact = fit(history, cutoff)
    artifact.update(approved=True, validation={"passed": True})
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(artifact))
    row = history[0]
    with override_settings(KORFBAL_SCORE_FORECAST_ARTIFACT=str(path)):
        assert serving.score_prediction(row, cutoff - timedelta(seconds=1)) is None
        prediction = serving.score_prediction(row, cutoff)
        assert prediction is not None
        assert prediction["status"] == "scored"
        assert prediction["samples"]["pool"] == len(history) // 3
        assert (
            prediction["expected_goals"]
            == summarize(prediction["rate_draws"])["expected_goals"]
        )
        broken = deepcopy(artifact)
        broken["contexts"][context_key(row)]["intercept"] = [float("nan")] * DRAW_COUNT
        with patch.object(serving, "load_artifact", return_value=broken):
            assert serving.score_prediction(row, cutoff) is None
    path = tmp_path / "candidate.json"
    artifact["approved"] = False
    path.write_text(json.dumps(artifact))
    assert serving.load_artifact(str(path)) is None
    assert serving.load_artifact(str(tmp_path / "missing.json")) is None


@pytest.mark.django_db
def test_export_and_summary_use_stable_identities_without_rating_configuration(
    predicted_match: Match,
    tmp_path: Path,
) -> None:
    """The existing summary transports scores independently of preseason ratings."""
    source = Match.objects.select_related("pool__competition_class__edition").get(
        pk=predicted_match.pk
    )
    source.playing_time_minutes = 40
    source.playing_time_observed_at = START
    source.save()
    row = features(source)
    assert row is not None
    result_score = 5
    ResultRevision.objects.create(
        match=source,
        observed_at=START,
        status="FINAL",
        home_score=5,
        away_score=4,
        automatic_result=False,
    )
    report = export_rows(str(source.season_id), START + timedelta(days=3))
    assert report["rows"][0]["season"] == str(
        source.pool.competition_class.edition.season_id
    )
    assert report["rows"][0]["revisions"][0]["home_score"] == result_score
    RatingConfiguration.objects.all().delete()
    native = predicted_match.local_match
    assert native is not None
    training = [
        {
            **row,
            "match": f"training-{index}",
            "starts_at": (START - timedelta(days=index + 2)).isoformat(),
            "duration_observed_at": (START - timedelta(days=40)).isoformat(),
            "revisions": [
                {
                    "revision": 1,
                    "observed_at": (START - timedelta(days=index + 1)).isoformat(),
                    "status": "FINAL",
                    "home_score": 8,
                    "away_score": 6,
                    "automatic_result": False,
                }
            ],
        }
        for index in range(12)
    ]
    artifact = fit(training, START)
    artifact.update(approved=True, validation={"passed": True})
    path = tmp_path / "approved-api.json"
    path.write_text(json.dumps(artifact))
    with override_settings(KORFBAL_SCORE_FORECAST_ARTIFACT=str(path)):
        response = APIClient().get(f"/api/matches/{native.pk}/summary/")
        assert response.status_code == HTTP_OK
        prediction = response.data["prediction"]
        assert prediction["status"] == "scored"
        assert prediction["duration"] == source.playing_time_minutes
        assert (
            prediction["expected_goals"]
            == summarize(prediction["rate_draws"])["expected_goals"]
        )
        assert prediction["context"]["pool_label"] == source.pool.name
    destination = tmp_path / "export.json"
    call_command(
        "export_score_forecasts", season=str(source.season_id), output=destination
    )
    assert json.loads(destination.read_text())["schema"] == 1


def test_training_command_saves_report_but_refuses_failed_approval(
    history: list[dict], tmp_path: Path
) -> None:
    """Operators receive diagnostic outputs even when a candidate cannot be promoted."""
    source, target, report = (
        tmp_path / name for name in ("source.json", "model.json", "report.json")
    )
    source.write_text(
        json.dumps({
            "schema": 1,
            "rows": history,
            "exported_at": (START + timedelta(days=26)).isoformat(),
            "metadata_history": "synthetic",
        })
    )
    with pytest.raises(CommandError, match="Promotion gate failed"):
        call_command(
            "fit_score_forecasts",
            "--origin",
            (START + timedelta(days=16)).isoformat(),
            "--origin",
            (START + timedelta(days=20)).isoformat(),
            input=source,
            output=target,
            report=report,
            cutoff=START + timedelta(days=25),
            approve=True,
        )
    assert json.loads(target.read_text())["approved"] is False
    assert json.loads(report.read_text())["passed"] is False


def test_frontend_fixture_matches_python_predictive_distribution() -> None:
    """The browser and API consume one checked-in synthetic mathematical contract."""
    path = Path(__file__).parents[6] / "fixtures/korfbal/score-forecast.json"
    fixture = json.loads(path.read_text())
    assert summarize(fixture["rates"]) == fixture["summary"]
