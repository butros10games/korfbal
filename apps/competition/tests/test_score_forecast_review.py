"""Human forecast decisions use frozen evidence and never deploy from Django."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, cast

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import CommandError, call_command
from django.test import Client, override_settings
from django.urls import reverse
import pytest
from rest_framework import status

from apps.competition.models import ScoreForecastReview
from apps.competition.offline.score_training import fit
from apps.competition.offline.score_validation import head_to_head


START = datetime(2026, 9, 1, tzinfo=UTC)
EXPECTED_SHARED_MATCHES = 59
EXPECTED_SHARED_POOLS = 12


@pytest.fixture
def score_rows() -> list[dict]:
    """Build enough chronological labels to fit both frozen artifacts."""
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
            "pool": f"pool-{index % 12}",
            "pool_label": "Synthetic",
            "home": f"home-{index % 12}",
            "away": f"away-{index % 12}",
            "duration": 60,
            "duration_observed_at": START.isoformat(),
            "starts_at": (START + timedelta(hours=12 * index)).isoformat(),
            "revisions": [
                {
                    "revision": 1,
                    "observed_at": (
                        START + timedelta(hours=12 * index + 2)
                    ).isoformat(),
                    "status": "FINAL",
                    "automatic_result": False,
                    "home_score": 13 + index % 4,
                    "away_score": 10 + index % 3,
                }
            ],
        }
        for index in range(150)
    ]


def approved_artifact(rows: list[dict], cutoff: datetime, available: datetime) -> dict:
    """Mark a deterministic fitted artifact as having passed its earlier gate."""
    artifact = fit(rows, cutoff)
    artifact.update(
        approved=True,
        available_from=available.isoformat(),
        input_sha256=f"input-{cutoff.isoformat()}",
        validation={"passed": True},
    )
    return artifact


def test_head_to_head_uses_only_shared_post_availability_matches(
    score_rows: list[dict],
) -> None:
    """Never compare a candidate on labels that existed before it was fitted."""
    incumbent = approved_artifact(
        score_rows, START + timedelta(days=20), START + timedelta(days=20, minutes=5)
    )
    candidate = approved_artifact(
        score_rows, START + timedelta(days=40), START + timedelta(days=40, minutes=5)
    )
    report = head_to_head(score_rows, incumbent, candidate, START + timedelta(days=70))
    assert report["available_from"] == candidate["available_from"]
    assert report["evaluated_matches"] == EXPECTED_SHARED_MATCHES
    assert report["evaluated_pools"] == EXPECTED_SHARED_POOLS
    assert report["verdict"] == "insufficient_evidence"
    assert report["metrics"]["candidate"]["matches"] == EXPECTED_SHARED_MATCHES


@pytest.mark.django_db
def test_register_review_checks_provenance_and_preserves_decisions(
    score_rows: list[dict], tmp_path: Path
) -> None:
    """Only aggregate matching evidence enters the application database."""
    incumbent = approved_artifact(
        score_rows, START + timedelta(days=20), START + timedelta(days=20, minutes=5)
    )
    candidate = approved_artifact(
        score_rows, START + timedelta(days=40), START + timedelta(days=40, minutes=5)
    )
    validation = {
        "passed": True,
        "metrics": {
            "candidate": {
                "matches": 101,
                "brier": 0.4,
                "score_log_loss": 5.2,
                "goal_mae": 2.8,
                "coverage_80": 0.82,
            }
        },
    }
    candidate["validation"] = validation
    audit = {
        "mode": "deployed-forward-audit",
        "artifact": {
            key: incumbent[key]
            for key in (
                "version",
                "input_sha256",
                "training_cutoff",
                "available_from",
            )
        },
        "metrics": {
            "candidate": {
                "matches": 100,
                "brier": 0.41,
                "score_log_loss": 5.3,
                "goal_mae": 2.9,
                "coverage_80": 0.84,
            }
        },
    }
    paths = {}
    for name, payload in {
        "incumbent": incumbent,
        "candidate": candidate,
        "validation": validation,
        "audit": audit,
    }.items():
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(payload))
    call_command(
        "register_score_forecast_review",
        incumbent=paths["incumbent"],
        candidate=paths["candidate"],
        validation=paths["validation"],
        forward_audit=paths["audit"],
        source_reference="forecast-runs/synthetic/result/candidate.json",
    )
    review = ScoreForecastReview.objects.get()
    assert review.artifact_sha256 == sha256(paths["candidate"].read_bytes()).hexdigest()
    assert review.status == "collecting"
    review.status = "approved_pending_activation"
    review.save(update_fields=["status"])
    with pytest.raises(CommandError, match="immutable"):
        call_command(
            "register_score_forecast_review",
            incumbent=paths["incumbent"],
            candidate=paths["candidate"],
            validation=paths["validation"],
            forward_audit=paths["audit"],
            source_reference="changed",
        )


@pytest.mark.django_db
def test_admin_review_requires_mfa_permission_note_and_head_to_head() -> None:
    """A staff session can inspect or decide only through explicit permissions."""
    review = ScoreForecastReview.objects.create(
        artifact_sha256="a" * 64,
        incumbent_sha256="b" * 64,
        source_reference="forecast-runs/synthetic/result/candidate.json",
        training_cutoff=START,
        available_from=START + timedelta(minutes=5),
        training_matches=7489,
        contexts=25,
        automated_passed=True,
        candidate_metrics={
            "brier": 0.44,
            "score_log_loss": 5.52,
            "goal_mae": 3.21,
            "coverage_80": 0.882,
        },
        incumbent_metrics={
            "brier": 0.40,
            "score_log_loss": 5.35,
            "goal_mae": 2.97,
            "coverage_80": 0.886,
        },
    )
    url = reverse("admin:competition_scoreforecastreview_change", args=[review.pk])
    user = cast(Any, get_user_model()).objects.create_user(
        username="reviewer", is_staff=True
    )
    user.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="competition",
            codename__in=[
                "view_scoreforecastreview",
                "decide_scoreforecastreview",
            ],
        )
    )
    client = Client()
    client.force_login(user)
    assert client.get(url).status_code == status.HTTP_302_FOUND
    client.force_login(user)
    session = client.session
    session["bg_auth_mfa_verified"] = user.get_session_auth_hash()
    session.save()
    response = client.get(url)
    assert response.status_code == status.HTTP_200_OK
    assert b"Waiting for an untouched head-to-head round" in response.content
    assert b"disabled" in response.content
    client.post(url, {"decision": "approve", "note": "Looks promising"})
    review.refresh_from_db()
    assert review.status == "collecting"
    client.post(url, {"decision": "reject", "note": "Need a safer interval"})
    review.refresh_from_db()
    assert review.status == "rejected"
    assert review.decision_history[-1]["action"] == "reject"
    review.head_to_head = {
        "verdict": "improved",
        "evaluated_matches": 125,
        "evaluated_pools": 18,
        "metrics": {
            "incumbent": review.incumbent_metrics,
            "candidate": review.candidate_metrics,
        },
    }
    review.save(update_fields=["head_to_head"])
    client.post(url, {"decision": "approve", "note": "Shared audit passed"})
    review.refresh_from_db()
    assert review.status == "approved_pending_activation"
    assert review.decided_by == user
    assert review.decision_history[-1]["action"] == "approve"


@pytest.mark.django_db
def test_activation_receipt_requires_exact_configured_approved_artifact(
    score_rows: list[dict], tmp_path: Path
) -> None:
    """A receipt proves host activation happened; it cannot perform activation."""
    artifact = approved_artifact(
        score_rows, START + timedelta(days=40), START + timedelta(days=40, minutes=5)
    )
    path = tmp_path / "immutable-candidate.json"
    path.write_text(json.dumps(artifact))
    digest = sha256(path.read_bytes()).hexdigest()
    review = ScoreForecastReview.objects.create(
        artifact_sha256=digest,
        incumbent_sha256="b" * 64,
        source_reference="forecast-runs/synthetic/result/candidate.json",
        training_cutoff=START + timedelta(days=40),
        available_from=START + timedelta(days=40, minutes=5),
        training_matches=artifact["training_matches"],
        contexts=len(artifact["contexts"]),
        automated_passed=True,
        status="approved_pending_activation",
    )
    with override_settings(KORFBAL_SCORE_FORECAST_ARTIFACT=str(path)):
        call_command("record_score_forecast_activation")
    review.refresh_from_db()
    assert review.status == "activated"
    assert review.decision_history[-1]["action"] == "activated"
    with (
        override_settings(KORFBAL_SCORE_FORECAST_ARTIFACT=str(path)),
        pytest.raises(CommandError, match="not approved for activation"),
    ):
        call_command("record_score_forecast_activation")
