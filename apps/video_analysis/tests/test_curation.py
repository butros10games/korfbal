"""Audit concurrency, reference corrections and leakage-free curated snapshots."""

from copy import deepcopy
from http import HTTPStatus
import json
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine import curation, vision
from apps.video_analysis.engine.store import ConflictError, Store, frame_version
from apps.video_analysis.models import Frame, ReviewAudit, Workspace
from apps.video_analysis.services import curation as service
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


def prepared(
    imported: tuple[User, DatabaseStore, Store],
) -> tuple[User, DatabaseStore, Frame, Workspace]:
    """Prepare one complete training frame without a model runtime."""
    owner, store, _ = imported
    frame = Frame.objects.select_related("recording__workspace").first()
    frame.status, frame.complete = "approved", True
    frame.correction = deepcopy(frame.proposal)
    frame.save()
    vision.assign_split(store, frame.recording.source_id, "train")
    return owner, store, frame, frame.recording.workspace


def payload(item: dict) -> dict:
    """Build a fully classified audit from server concurrency fields."""
    return {
        "id": item["id"],
        "source": "original",
        "frame_version": item["frame_version"],
        "comparison": item["comparison"],
        "audit_revision": item["audit"].get("revision", 0),
        "tags": ["occlusion"],
        "causes": {
            r["id"]: "bad_box" for r in item["scores"]["rows"] if not r["matched"]
        },
        "complete": True,
        "selected": True,
    }


def test_audit_preserves_labels_and_rejects_stale_writers(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Reject stale audits and retain decisions across older background saves."""
    owner, store, frame, workspace = prepared(imported)
    old_state = store.read()
    item = service.listing(workspace, store, {"id": frame.pk})["frame"]
    result = service.save(workspace, store, owner, payload(item))["frame"]
    assert result["ready"]
    assert result["frame_version"] == item["frame_version"]
    with pytest.raises(ConflictError):
        service.save(workspace, store, owner, payload(item))
    with store.transaction():
        store._persist(old_state)
    current = store.read()["matches"][0]["frames"][0]
    assert current["curation"]["selected"]
    assert current["correction"] == frame.correction
    assert ReviewAudit.objects.filter(payload__action="curation").count() == 1
    frame.refresh_from_db()
    frame.correction["notes"] = "New human correction"
    frame.save()
    assert not service.listing(workspace, store, {"id": frame.pk})["frame"]["ready"]


def test_curation_schedules_export_without_changing_annotation_version(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Curated training selections are exported even if no labels are edited."""
    owner, store, frame, workspace = prepared(imported)
    item = service.listing(workspace, store, {"id": frame.pk})["frame"]

    result = service.save(workspace, store, owner, payload(item))["frame"]

    assert result["frame_version"] == item["frame_version"]
    job = BackgroundJob.objects.get(task="apps.video_analysis.tasks.publish_reviews")
    assert job.args == [str(workspace.pk)]
    assert job.queue == "vision"
    assert job.due_at is not None


def test_held_out_selection_and_incomplete_reference_are_blocked(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Do not select held-out footage or certify incomplete references."""
    owner, store, frame, workspace = prepared(imported)
    vision.assign_split(store, frame.recording.source_id, "val")
    item = service.listing(workspace, store, {"id": frame.pk})["frame"]
    with pytest.raises(ValueError, match="Only Training"):
        service.save(workspace, store, owner, payload(item))
    vision.assign_split(store, frame.recording.source_id, "train")
    frame.complete = False
    frame.save()
    item = service.listing(workspace, store, {"id": frame.pk})["frame"]
    with pytest.raises(ValueError, match="Approve complete"):
        service.save(workspace, store, owner, payload(item))


def test_unmatched_and_uncertain_boxes_need_resolution(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Require explicit classification and resolution of all unmatched boxes."""
    owner, store, frame, workspace = prepared(imported)
    frame.proposal["objects"] = []
    frame.save()
    item = service.listing(workspace, store, {"id": frame.pk})["frame"]
    assert item["scores"]["unmatched_references"] > 0
    draft = payload(item)
    for causes in ({}, {r["id"]: "uncertain" for r in item["scores"]["rows"]}):
        with pytest.raises(ValueError, match="resolve every"):
            service.save(workspace, store, owner, dict(draft, causes=causes))
    assert service.save(workspace, store, owner, draft)["frame"]["ready"]


def test_api_requires_mfa_csrf_and_scopes_frames(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Enforce authentication, CSRF and workspace frame lookup."""
    owner, _store, frame, _workspace = prepared(imported)
    assert (
        Client().get("/video-analysis/curation").status_code == HTTPStatus.UNAUTHORIZED
    )
    client = verified(owner)
    response = client.get(f"/video-analysis/curation?id={frame.pk}")
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    body = payload(data["frame"])
    assert (
        client.post(
            "/video-analysis/curation", body, content_type="application/json"
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    assert (
        client.post(
            "/video-analysis/curation",
            body,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=data["csrf"],
        ).status_code
        == HTTPStatus.OK
    )
    assert (
        client.post(
            "/video-analysis/curation",
            dict(body, id=999999),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=data["csrf"],
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )


def test_curated_selection_preserves_validation_and_requires_current_audits() -> None:
    """Freeze selected training frames and unchanged held-out partitions."""

    def frame(name: str, selected: bool) -> dict:
        f = {
            "id": name,
            "status": "approved",
            "complete": True,
            "correction": {"objects": []},
        }
        f["curation"] = {
            "selected": selected,
            "complete": True,
            "frame_version": frame_version(f),
        }
        return f

    chosen, other, held = (
        frame("chosen", True),
        frame("other", False),
        frame("held", False),
    )
    data = {
        "matches": [
            {"id": "train", "frames": [chosen, other]},
            {"id": "held", "frames": [held]},
        ]
    }
    mapping = {"train": "train", "held": "val"}
    selected = vision.select_frames(data, mapping, "people", "curated")
    assert [(m["id"], f["id"]) for m, f in selected] == [
        ("train", "chosen"),
        ("held", "held"),
    ]
    chosen["correction"]["notes"] = "changed"
    with pytest.raises(ValueError, match="stale"):
        vision.select_frames(data, mapping, "people", "curated")
    assert [
        f["id"] for _, f in vision.select_frames(data, mapping, "people", "all")
    ] == ["chosen", "other", "held"]


@pytest.mark.parametrize(
    ("split", "synthetic"),
    [("pool", False), ("val", False), ("test", False), ("train", True)],
)
def test_stale_selected_audit_outside_training_does_not_block_curated_snapshot(
    split: str,
    synthetic: bool,
) -> None:
    """Moving a previously selected match out of training drops its audit gate."""
    frame = {
        "id": "frame",
        "status": "approved",
        "complete": True,
        "correction": {"objects": []},
    }
    chosen = dict(frame)
    chosen["curation"] = {
        "selected": True,
        "complete": True,
        "frame_version": frame_version(chosen),
    }
    moved = dict(frame, curation={"selected": True, "complete": False})
    data = {
        "matches": [
            {"id": "train", "frames": [chosen]},
            {"id": "held", "frames": [frame]},
            {"id": "moved", "synthetic": synthetic, "frames": [moved]},
        ]
    }

    selected = vision.select_frames(
        data, {"train": "train", "held": "val", "moved": split}, "people", "curated"
    )

    assert [m["id"] for m, _ in selected] == (
        ["train", "held"]
        if split == "pool" or synthetic
        else ["train", "held", "moved"]
    )


def test_import_benchmark_is_immutable_and_keeps_model_drafts_separate(
    imported: tuple[User, DatabaseStore, Store], tmp_path: Path
) -> None:
    """Import audit-only evidence without adding it to model draft queues."""
    _owner, store, frame, workspace = prepared(imported)
    report = {
        "weights_sha256": "fixed-model",
        "frames": [
            {
                "match_id": frame.recording.source_id,
                "frame_id": frame.source_id,
                "baseline": frame.proposal,
                "reference": frame.correction,
                "group": "outside_training",
            }
        ],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    call_command("import_video_benchmark", str(path), name="fixed-comparison")
    source = "benchmark:fixed-comparison"
    item = service.listing(workspace, store, {"source": source, "id": frame.pk})[
        "frame"
    ]
    assert item["has_prediction"]
    assert not item["reference_changed"]
    assert vision.proposal_report(store, "latest") == {"frames": []}
    with pytest.raises(ValueError, match="already exists"):
        call_command("import_video_benchmark", str(path), name="fixed-comparison")


def test_matching_is_one_to_one_class_aware_and_maximum_cardinality() -> None:
    """Recover both matches when a greedy first choice would miss one."""

    def box(x: float, label: str = "player") -> dict:
        return {"label": label, "bbox": [x, 0.1, 0.2, 0.4], "confidence": 0.9}

    result = curation.compare(
        {"objects": [box(0.13), box(0.1), box(0.6, "referee")]},
        {"objects": [box(0.1), box(0.18), box(0.6)]},
    )
    assert result["matched"] == len(["first", "second"])
    assert result["unmatched_predictions"] == result["unmatched_references"] == 1


def test_mutable_model_outputs_cannot_be_audited(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Audit a named completed run, never a moving latest or partial output."""
    _, store, _, _ = prepared(imported)
    run, _ = vision.start_run(store, "propose")
    (run / "predictions.json").write_text('{"frames": []}')
    assert service.sources(store) == [{"id": "original", "title": "Original proposals"}]
    with pytest.raises(ValueError, match="finish"):
        service.predictions(store, run.name)
    with pytest.raises(ValueError, match="specific"):
        service.predictions(store, "latest")
