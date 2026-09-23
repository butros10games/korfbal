"""Native clip launch, result authorization, cancellation and failure receipts."""

from http import HTTPStatus
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import uuid

from django.contrib.auth.models import User
from django.test import Client
import pytest

from apps.video_analysis.adapters.detector import clip
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.clip_contract import MAX_RUNTIME_SECONDS
from apps.video_analysis.engine.clip_models import MODEL_ERROR
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.models import AnalysisJob, Recording, Workspace
from apps.video_analysis.tasks import execute
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


def ready(store: DatabaseStore) -> dict:
    """Register synthetic footage and a completed model without loading a detector."""
    recording = Recording.objects.get(source_id="demo")
    recording.metadata.update(video="demo/synthetic.mp4", duration_seconds=60)
    recording.save()
    video = store.root / "demo/synthetic.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"synthetic-video")
    weights = store.root / "vision/runs/model/fit/weights/best.pt"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"synthetic-checkpoint")
    atomic_json(
        store.root / "vision/runs/model/run.json",
        {
            "kind": "train",
            "status": "completed",
            "classes": ["ball", "player", "basket", "referee"],
        },
    )
    return {
        "request_id": str(uuid.uuid4()),
        "model": "model",
        "match_id": "demo",
        "options": {"start": 10, "duration": 20},
    }


def test_launch_mfa_csrf_limits_idempotency_and_no_frame_read(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Accept one request without blocking web workers on labels or inference."""
    owner, store, _ = imported
    payload = ready(store)
    anonymous = Client()
    assert anonymous.get("/video-analysis/clips").status_code == HTTPStatus.UNAUTHORIZED
    anonymous.force_login(owner)
    assert anonymous.get("/video-analysis/clips").status_code == HTTPStatus.FORBIDDEN
    client = verified(owner)
    csrf = client.get("/video-analysis/clips").json()["csrf"]
    assert (
        client.post(
            "/video-analysis/clips", payload, content_type="application/json"
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    with (
        patch.object(
            DatabaseStore, "read", side_effect=AssertionError("Full frame read")
        ),
        patch("apps.video_analysis.services.jobs.enqueue") as publish,
    ):
        first = client.post(
            "/video-analysis/clips",
            payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        retry = client.post(
            "/video-analysis/clips",
            payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        assert first.status_code == HTTPStatus.ACCEPTED
        assert retry.json() == first.json()
        assert publish.call_count == 1
        altered = dict(payload, options={"duration": 5})
        assert (
            client.post(
                "/video-analysis/clips",
                altered,
                content_type="application/json",
                HTTP_X_CSRFTOKEN=csrf,
            ).status_code
            == HTTPStatus.CONFLICT
        )
        invalid = dict(
            payload, request_id=str(uuid.uuid4()), options={"duration": 600, "fps": 25}
        )
        assert (
            client.post(
                "/video-analysis/clips",
                invalid,
                content_type="application/json",
                HTTP_X_CSRFTOKEN=csrf,
            ).status_code
            == HTTPStatus.BAD_REQUEST
        )
        assert (
            client.get("/video-analysis/clips").json()["runs"][0]["status"] == "queued"
        )
    assert AnalysisJob.objects.count() == 1


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "classes",
    [None, ["player", "basket", "referee"], ["ball", "player", "basket", "referee"]],
)
def test_clip_model_taxonomy_is_checked_before_queueing(
    imported: tuple[User, DatabaseStore, Store], legacy: bool, classes: list[str] | None
) -> None:
    """Reject the old pilot without loading weights; accept an exact frozen taxonomy."""
    owner, store, _ = imported
    payload = ready(store)
    record = {"kind": "train", "status": "completed", "snapshot": "pilot"}
    if legacy:
        atomic_json(
            store.root / "vision/snapshots/pilot/manifest.json", {"classes": classes}
        )
    else:
        record["classes"] = classes
    atomic_json(store.root / "vision/runs/model/run.json", record)
    client = verified(owner)
    csrf = client.get("/video-analysis/clips").json()["csrf"]
    with patch("apps.video_analysis.services.jobs.enqueue") as enqueue:
        response = client.post(
            "/video-analysis/clips",
            payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
    supported = classes is not None and "ball" in classes
    assert response.status_code == (
        HTTPStatus.ACCEPTED if supported else HTTPStatus.BAD_REQUEST
    )
    assert enqueue.called == supported
    assert AnalysisJob.objects.exists() == supported
    if not supported:
        assert response.json()["error"] == MODEL_ERROR


@pytest.mark.parametrize("code", ["incompatible_model", "private-path-or-unknown"])
def test_clip_failure_survives_subprocess_task_and_listing(
    imported: tuple[User, DatabaseStore, Store], code: str
) -> None:
    """Propagate only known public reasons and retain the model in saved recipes."""
    owner, store, _ = imported
    payload = ready(store)
    job = AnalysisJob.objects.create(
        workspace_id=store.workspace_id,
        requested_by=owner,
        kind="clip",
        payload=payload,
    )
    root = store.root / "vision/clips" / str(job.pk)

    def fail(command: list[str], **_kwargs: object) -> None:
        atomic_json(
            root / "run.json",
            {
                "status": "failed",
                "chunks": [],
                "failure_code": code,
                "message": "/private/worker/secret",
                "recipe": {"match_id": "demo", "options": payload["options"]},
            },
        )
        raise subprocess.CalledProcessError(1, command)

    with (
        patch("apps.video_analysis.tasks.worker_store", return_value=store),
        patch("apps.video_analysis.tasks.run_clip", side_effect=clip),
        patch("apps.video_analysis.adapters.detector.subprocess.run", side_effect=fail),
    ):
        execute(str(job.pk))
    job.refresh_from_db()
    assert job.status == "failed"
    assert "/private/" not in job.message
    if code == "incompatible_model":
        assert job.message == MODEL_ERROR
    row = verified(owner).get("/video-analysis/clips").json()["runs"][0]
    assert row["message"] == job.message
    assert row["recipe"]["model"] == "model"


def test_result_scoping_allowlist_and_queued_cancellation(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A run ID is not sufficient to read another workspace or arbitrary files."""
    owner, store, _ = imported
    payload = ready(store)
    client = verified(owner)
    csrf = client.get("/video-analysis/clips").json()["csrf"]
    with patch("apps.video_analysis.services.jobs.enqueue"):
        response = client.post(
            "/video-analysis/clips",
            payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
    identity = response.json()["job_id"]
    root = store.root / "vision/clips" / identity
    atomic_json(
        root / "run.json",
        {
            "id": identity,
            "chunks": [{"name": "chunk-00000.json"}],
            "status": "completed",
        },
    )
    atomic_json(root / "chunk-00000.json", {"frames": [{"time_seconds": 10}]})
    url = f"/video-analysis/clips/result?run={identity}"
    assert client.get(url + "&chunk=chunk-00000.json").json()["frames"]
    assert (
        client.get(url + "&chunk=../../review.json").status_code == HTTPStatus.NOT_FOUND
    )
    other = Workspace.objects.create(slug="other", owner=owner)
    foreign = AnalysisJob.objects.create(
        workspace=other, requested_by=owner, kind="clip"
    )
    assert (
        client.get(f"/video-analysis/clips/result?run={foreign.pk}").status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        client.get("/video-analysis/clips/result?run=not-a-uuid").status_code
        == HTTPStatus.BAD_REQUEST
    )
    assert (
        client.get("/video-analysis/clips/cancel").status_code
        == HTTPStatus.METHOD_NOT_ALLOWED
    )
    assert (
        client.post(
            "/video-analysis/clips/cancel",
            {"run_id": identity},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == HTTPStatus.OK
    )
    job = AnalysisJob.objects.get(pk=identity)
    assert job.status == "cancelled"
    with patch("apps.video_analysis.tasks.run_clip") as worker:
        execute(str(job.pk))
        worker.assert_not_called()


def test_worker_uses_fresh_database_input_and_publishes_partial_failure(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Ignore legacy caches and preserve produced chunks on subprocess failure."""
    _, store, _ = imported
    payload = ready(store)
    payload["options"] = {"start": 10, "duration": 5}
    store.path.write_text('{"matches": []}', encoding="utf-8")
    run_id = str(uuid.uuid4())
    root = store.root / "vision/clips" / run_id

    def fail(command: list[str], **kwargs: object) -> None:
        projection = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        assert projection["match"]["id"] == "demo"
        assert "frames" not in projection["match"]
        assert kwargs["timeout"] == MAX_RUNTIME_SECONDS + 60
        atomic_json(root / "run.json", {"status": "failed", "chunks": []})
        atomic_json(root / "chunk-00000.json", {"frames": []})
        raise RuntimeError("Synthetic inference failure")

    with (
        patch("apps.video_analysis.adapters.detector.subprocess.run", side_effect=fail),
        patch.object(store, "publish_artifact") as publish,
        pytest.raises(RuntimeError, match="Synthetic"),
    ):
        clip(store, run_id, payload)
    assert {call.args[0] for call in publish.call_args_list} == {
        f"vision/clips/{run_id}/run.json",
        f"vision/clips/{run_id}/chunk-00000.json",
    }


def test_timestamped_references_are_bound_to_recording_and_idempotent_recipe(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Accept off-clip anchors, reject out-of-footage anchors and changed retries."""
    owner, store, _ = imported
    payload = ready(store)
    court = {
        "length": 40,
        "width": 20,
        "anchors": [
            {
                "time": 40,
                "points": [
                    {"image": [0.1 + 0.8 * x, 0.1 + 0.8 * y], "court": [x, y]}
                    for x, y in [(0, 0), (1, 0), (1, 1), (0, 1)]
                ],
            }
        ],
    }
    payload["options"]["court"] = court
    client = verified(owner)
    csrf = client.get("/video-analysis/clips").json()["csrf"]

    def submit() -> int:
        return client.post(
            "/video-analysis/clips",
            payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code

    with patch("apps.video_analysis.services.jobs.enqueue"):
        assert submit() == HTTPStatus.ACCEPTED
        assert submit() == HTTPStatus.ACCEPTED
        court["anchors"][0]["time"] = 41
        assert submit() == HTTPStatus.CONFLICT
        payload["request_id"] = str(uuid.uuid4())
        court["anchors"][0]["time"] = 60
        assert submit() == HTTPStatus.BAD_REQUEST
    assert AnalysisJob.objects.count() == 1
