"""Bound interactive work while retaining authoritative concurrency and recovery."""

from copy import deepcopy
from http import HTTPStatus
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
import pytest
from pytest_django.fixtures import Settings

from apps.kwt_common.models import BackgroundJob
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine import vision
from apps.video_analysis.engine.store import Store, atomic_json, frame_version
from apps.video_analysis.models import Frame, Workspace
from apps.video_analysis.services import review
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


def test_export_intent_failure_rolls_back_frame_and_revision(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """An acknowledged review cannot be separated from durable export recovery."""
    owner, store, _ = imported
    before = store.read()
    match = before["matches"][0]
    frame = match["frames"][0]
    with (
        patch(
            "apps.video_analysis.services.review.enqueue",
            side_effect=RuntimeError("Cannot persist job"),
        ),
        pytest.raises(RuntimeError, match="Cannot persist job"),
    ):
        review.save(
            Workspace.objects.get(pk=store.workspace_id),
            owner,
            {
                "action": "review",
                "match_id": match["id"],
                "frame_id": frame["id"],
                "expected_frame_version": frame_version(frame),
                "annotation": frame["proposal"],
                "status": "approved",
                "complete": True,
            },
        )
    assert store.read() == before


def test_timing_is_scoped_and_rejects_stale_workspace_revision(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Timing edits publish asynchronously and preserve optimistic concurrency."""
    owner, store, _ = imported
    before = store.read()
    workspace = Workspace.objects.get(pk=store.workspace_id)
    payload = {
        "action": "timing",
        "match_id": before["matches"][0]["id"],
        "revision": before["revision"],
        "start_seconds": 10,
    }
    with patch.object(
        type(store), "read", side_effect=AssertionError("Full dataset read")
    ):
        result = review.save(workspace, owner, payload)
    assert result["revision"] == before["revision"] + 1
    assert store.read()["matches"][0]["match_start_seconds"] == payload["start_seconds"]
    with pytest.raises(ValueError, match="Another review was saved"):
        review.save(workspace, owner, payload)


def test_reads_do_not_lock_and_state_loads_only_selected_recording(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Unrelated recordings remain navigable without transferring their labels."""
    owner, store, _ = imported
    data = store.read()
    match = deepcopy(data["matches"][0])
    match["id"] = "second"
    store.add_match(match)
    with CaptureQueriesContext(connection) as queries:
        result = store.read()
    assert len(queries) == 1
    assert not any("FOR UPDATE" in q["sql"] for q in queries)
    assert len(result["matches"]) == len(data["matches"]) + 1
    client = verified(owner)
    with patch.object(
        type(store), "read", side_effect=AssertionError("Full workspace read")
    ):
        response = client.get("/video-analysis/state?scope=recording&match=second")
    assert response.status_code == HTTPStatus.OK
    rows = response.json()["matches"]
    assert len(rows) == len(result["matches"])
    assert not rows[0]["frames"]
    assert rows[0]["frame_count"] == len(match["frames"])
    assert rows[1]["frames"][0]["frame_version"] == frame_version(match["frames"][0])
    for frame in Frame.objects.filter(recording__source_id="second"):
        frame.metadata = dict(frame.metadata, dataset_decision="removed")
        frame.save(update_fields=["metadata"])
    restored = client.get("/video-analysis/state?scope=recording&match=second").json()
    assert restored["matches"][0]["frames"]
    assert not restored["matches"][1]["frames"]


def test_review_response_is_scoped_and_export_intent_is_durable(
    imported: tuple[User, DatabaseStore, Store], settings: Settings
) -> None:
    """A slow/unavailable bucket cannot block a review or erase its recovery intent."""
    owner, store, _ = imported
    client = verified(owner)
    state = client.get("/video-analysis/state").json()
    match = state["matches"][0]
    frame = match["frames"][0]
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    with (
        patch("apps.video_analysis.adapters.objects.object_client"),
        patch(
            "apps.video_analysis.adapters.objects.WorkspaceObjects.hydrate_artifacts",
            side_effect=AssertionError("Artifact hydration"),
        ),
        patch.object(
            type(store), "read", side_effect=AssertionError("Full workspace read")
        ),
        patch(
            "apps.video_analysis.adapters.objects.WorkspaceObjects.publish_review",
            side_effect=AssertionError("Synchronous export"),
        ),
    ):
        response = client.post(
            "/video-analysis/review",
            {
                "action": "review",
                "match_id": match["id"],
                "frame_id": frame["id"],
                "expected_frame_version": frame["frame_version"],
                "annotation": dict(frame["proposal"], notes="saved without S3"),
                "status": "approved",
                "complete": True,
            },
            content_type="application/json",
            HTTP_X_CSRFTOKEN=state["csrf"],
        )
        assert client.get("/video-analysis/job").status_code == HTTPStatus.OK
    assert response.status_code == HTTPStatus.OK
    saved = response.json()["frame"]
    assert saved["correction"]["notes"] == "saved without S3"
    assert saved["frame_version"] == frame_version(saved)
    assert len(saved["history"]) == 1
    job = BackgroundJob.objects.get(task="apps.video_analysis.tasks.publish_reviews")
    assert job.args == [str(store.workspace_id)]
    assert job.queue == "vision"
    assert job.due_at is not None


def test_proposal_cache_invalidates_on_new_outputs_and_protects_cached_values(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Frame navigation parses once and sees replacement batches immediately."""
    _, store, _ = imported
    root = store.root / "vision/runs/batch"
    record = {"match_id": "demo", "frame_id": "one", "prediction": {"objects": []}}
    atomic_json(
        root / "run.json", {"id": "batch", "created_at": 1, "status": "running"}
    )
    atomic_json(root / "predictions.json", {"frames": [record]})
    assert not vision.read_prediction(store, "latest", "demo", "one")
    atomic_json(
        root / "run.json", {"id": "batch", "created_at": 1, "status": "completed"}
    )
    first = vision.read_prediction(store, "latest", "demo", "one")
    first["prediction"]["objects"].append({"label": "ball"})
    with patch(
        "apps.video_analysis.engine.vision.json.loads",
        side_effect=AssertionError("Reparsed proposals"),
    ):
        assert vision.read_prediction(store, "latest", "demo", "one")["prediction"] == {
            "objects": []
        }
    priority = 20
    atomic_json(
        root / "predictions.json", {"frames": [dict(record, priority=priority)]}
    )
    assert (
        vision.read_prediction(store, "latest", "demo", "one")["priority"] == priority
    )
