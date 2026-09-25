"""Paid launch authorization, policy binding, retries and local cancellation."""

from dataclasses import asdict
from http import HTTPStatus
import json
from pathlib import Path
import time
from typing import Any
from unittest.mock import patch
import uuid

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import Client
import pytest
from pytest_django.fixtures import Settings

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.adapters.training import policy_version
from apps.video_analysis.engine.remote.controller import Policy
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.models import AnalysisJob
from apps.video_analysis.tasks import perform
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("price", [None, 1.25])
def test_paid_launch_requires_mfa_csrf_and_is_idempotent(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    tmp_path: Path,
    price: float | None,
) -> None:
    """A response retry cannot create a second paid job or override its budget."""
    owner, store, _ = imported
    policy = Policy(image="worker@sha256:" + "a" * 64, enabled=True)
    path = tmp_path / "policy.json"
    atomic_json(path, asdict(policy))
    settings.VIDEO_ANALYSIS_TRAINING_POLICY = str(path)
    atomic_json(
        store.root / "vision/remote/controller.json",
        {
            "checked_at": time.time(),
            "healthy": True,
            "enabled": True,
            "cloud_configured": True,
        },
    )
    payload = {
        "request_id": str(uuid.uuid4()),
        "snapshot": "pilot",
        "epochs": 10,
        "policy_version": policy_version(policy),
        "policy": {"max_hourly_usd": 999},
    }
    if price is not None:
        payload.update(max_hourly_usd=price, base_weights="yolo26m.pt")
    url = "/video-analysis/vision/train"
    anonymous = Client()
    assert (
        anonymous.post(url, payload, content_type="application/json").status_code
        == HTTPStatus.UNAUTHORIZED
    )
    anonymous.force_login(owner)
    assert (
        anonymous.post(url, payload, content_type="application/json").status_code
        == HTTPStatus.FORBIDDEN
    )
    client = verified(owner)
    atomic_json(store.root / "vision/snapshots/pilot/manifest.json", {})
    csrf = client.get("/video-analysis/state").json()["csrf"]
    assert (
        client.post(url, payload, content_type="application/json").status_code
        == HTTPStatus.FORBIDDEN
    )
    with (
        patch("apps.video_analysis.tasks.dataset_report") as report,
        patch("apps.video_analysis.services.jobs.enqueue") as dispatch,
    ):

        def post(body: dict[str, Any]) -> HttpResponse:
            return client.post(
                url, body, content_type="application/json", HTTP_X_CSRFTOKEN=csrf
            )

        assert (
            post(dict(payload, policy_version="stale")).status_code
            == HTTPStatus.CONFLICT
        )
        assert post(dict(payload, epochs=True)).status_code == HTTPStatus.BAD_REQUEST
        for bad in (0, -1, True, "1", None, 11):
            assert (
                post(dict(payload, max_hourly_usd=bad)).status_code
                == HTTPStatus.BAD_REQUEST
            )
        assert (
            post(dict(payload, base_weights="arbitrary.pt")).status_code
            == HTTPStatus.BAD_REQUEST
        )
        first = post(payload)
        assert first.status_code == HTTPStatus.ACCEPTED
        job = AnalysisJob.objects.get(pk=first.json()["job_id"])
        expected = dict(asdict(policy), max_hourly_usd=price or policy.max_hourly_usd)
        assert job.payload["policy"] == expected
        if price is not None:
            assert job.payload["base_weights"] == "yolo26m.pt"
        assert post(dict(payload, max_hourly_usd=2)).status_code == HTTPStatus.CONFLICT
        assert (
            post(dict(payload, base_weights="yolo26s.pt")).status_code
            == HTTPStatus.CONFLICT
        )
        job.status = "completed"
        job.save()
        assert post(payload).json() == first.json()
        assert post(dict(payload, epochs=30)).status_code == HTTPStatus.CONFLICT
        assert dispatch.call_count == 1
        report.assert_not_called()
        assert AnalysisJob.objects.count() == 1
        atomic_json(store.root / "vision/remote/controller.json", {"checked_at": 0})
        assert (
            post(dict(payload, request_id=str(uuid.uuid4()))).status_code
            == HTTPStatus.CONFLICT
        )


def test_cancel_only_updates_local_workspace_job(
    imported: tuple[User, DatabaseStore, Store], settings: Settings, tmp_path: Path
) -> None:
    """Cancellation is MFA/CSRF protected and leaves provider deletion to the daemon."""
    owner, store, _ = imported
    path = tmp_path / "policy.json"
    atomic_json(path, asdict(Policy(image="worker@sha256:" + "a" * 64)))
    settings.VIDEO_ANALYSIS_TRAINING_POLICY = str(path)
    job_id = uuid.uuid4().hex
    record = store.root / "vision/remote" / job_id / "job.json"
    atomic_json(record, {"id": job_id, "status": "running", "cancel_requested": False})
    client = verified(owner)
    csrf = client.get("/video-analysis/state").json()["csrf"]
    response = client.post(
        "/video-analysis/vision/cancel",
        {"job_id": job_id},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert response.status_code == HTTPStatus.OK
    assert json.loads(record.read_text(encoding="utf-8"))["cancel_requested"] is True


def test_invalid_training_inputs_never_reach_paid_controller(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Queued preparation must verify the dataset before cloud allocation."""
    _, store, _ = imported
    job = AnalysisJob(kind="train", payload={"snapshot": "pilot"})
    with (
        patch(
            "apps.video_analysis.tasks.dataset_report",
            side_effect=ValueError("Corrupt snapshot"),
        ),
        patch("apps.video_analysis.tasks.queue_training") as queue,
        pytest.raises(ValueError, match="Corrupt snapshot"),
    ):
        perform(job, store)
    queue.assert_not_called()
