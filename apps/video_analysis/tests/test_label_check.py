"""Label checks after training and the mixed cross-recording review queue."""

from http import HTTPStatus
import json
from typing import Any

from django.contrib.auth.models import User
import pytest

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine import label_check as engine
from apps.video_analysis.engine.store import Store, atomic_json, frame_version
from apps.video_analysis.models import Frame, Recording, Workspace
from apps.video_analysis.queries import QUEUE_BATCH, frame_payload
from apps.video_analysis.services import label_check

from .test_review import verified


pytestmark = pytest.mark.django_db
SCORE = 1.4
DOUBTFUL = 5


def box(label: str, bbox: list[float], **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    """One annotation object."""
    return {"label": label, "bbox": bbox, **extra}


def annotation(*objects: dict[str, Any]) -> dict[str, Any]:
    """Build a reviewed live frame."""
    return {"scene": "live", "event": "none", "notes": "", "objects": list(objects)}


def test_disagreements_name_each_kind_of_label_problem() -> None:
    """Confident model boxes and ignored labels become ranked, anchored hints."""
    reference = annotation(
        box("player", [0.1, 0.1, 0.1, 0.3]),
        box("player", [0.3, 0.1, 0.1, 0.3]),
        box("player", [0.5, 0.1, 0.1, 0.3]),
        box("player", [0.7, 0.1, 0.1, 0.3]),
        box("basket", [0.85, 0.1, 0.05, 0.1]),
    )
    prediction = annotation(
        box("player", [0.1, 0.1, 0.1, 0.3], confidence=0.9),
        # A player labelled as referee by the model.
        box("referee", [0.3, 0.1, 0.1, 0.3], confidence=0.8),
        # Overlaps the third label only loosely.
        box("player", [0.54, 0.12, 0.1, 0.3], confidence=0.7),
        # Nobody labelled this person.
        box("player", [0.1, 0.6, 0.1, 0.3], confidence=0.95),
        # Too unsure to call a missing label.
        box("player", [0.4, 0.6, 0.1, 0.3], confidence=0.3),
        box("basket", [0.85, 0.1, 0.05, 0.1], confidence=0.9, post_foot=[0.87, 0.5]),
    )
    reasons = engine.disagreements(
        prediction, reference, ["player", "referee", "basket"], feet=True
    )
    kinds = [r["kind"] for r in reasons]
    assert kinds == [
        "unlabelled",
        "wrong_role",
        "unseen_label",
        "loose_box",
        "missing_post_foot",
    ]
    assert reasons[0]["bbox"] == [0.1, 0.6, 0.1, 0.3]
    assert reasons[2]["bbox"] == [0.7, 0.1, 0.1, 0.3]
    assert engine.score(reasons) == pytest.approx(1.5 * (0.95 + 0.8) + 1 + 0.5 + 0.5)
    assert engine.disagreements(reference, reference, ["player", "basket"]) == []


def test_check_ranks_frozen_frames_by_disagreement(tmp_path: Any) -> None:  # noqa: ANN401
    """The report keeps each frame's frozen identity so stale flags can be dropped."""
    clean = annotation(box("player", [0.1, 0.1, 0.1, 0.3]))
    manifest = {
        "id": "snapshot",
        "classes": ["player", "referee"],
        "frames": [
            {
                "match_id": "m",
                "frame_id": f"f{i}",
                "frame_version": f"v{i}",
                "split": "train",
                "image": f"images/train/{i}.jpg",
                "annotation": clean,
            }
            for i in range(2)
        ],
    }

    def predict(image: Any) -> dict[str, Any]:  # noqa: ANN401
        extra = [box("player", [0.5, 0.5, 0.1, 0.3], confidence=0.9)]
        return annotation(
            box("player", [0.1, 0.1, 0.1, 0.3], confidence=0.9),
            *(extra if image.stem == "1" else []),
        )

    report = engine.check(tmp_path, manifest, predict)
    assert [f["frame_id"] for f in report["frames"]] == ["f1", "f0"]
    assert report["frames"][0]["frame_version"] == "v1"
    assert report["frames"][1]["score"] == 0


def write_report(
    store: Store, frames: list[dict[str, Any]], run: str = "remote-a"
) -> None:
    """Place a returned training run with its label ranking in the store."""
    root = store.root / "vision/runs" / run
    atomic_json(
        root / "run.json",
        {"id": run, "kind": "train", "status": "completed", "created_at": run},
    )
    atomic_json(
        root / "label_check.json", {"version": 1, "snapshot": "s", "frames": frames}
    )


def approve(frame: Frame, labels: dict[str, Any]) -> None:
    """Record a human approval directly."""
    frame.correction = labels
    frame.status = "approved"
    frame.complete = True
    frame.save(update_fields=["correction", "status", "complete"])


def test_flags_follow_the_latest_run_and_lapse_when_the_frame_is_saved(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Flags never touch the fingerprint, and saving the frame is the check."""
    owner, store, _ = imported
    workspace = Workspace.objects.get()
    frames = list(Frame.objects.order_by("position")[:3])
    labels = annotation(box("player", [0.1, 0.1, 0.1, 0.3], confidence=1.0))
    for frame in frames:
        approve(frame, labels)
    version = frame_version(frame_payload(frames[0]))
    match_id = frames[0].recording.source_id
    reasons = [engine.reason("unlabelled", box("player", [0.5, 0.5, 0.1, 0.3]), 0.9)]
    write_report(
        store,
        [
            {
                "match_id": match_id,
                "frame_id": frames[0].source_id,
                "frame_version": version,
                "score": SCORE,
                "reasons": reasons,
            },
            # Changed after the snapshot: never flagged.
            {
                "match_id": match_id,
                "frame_id": frames[1].source_id,
                "frame_version": "stale",
                "score": 3.0,
                "reasons": reasons,
            },
            # Below the bar.
            {
                "match_id": match_id,
                "frame_id": frames[2].source_id,
                "frame_version": frame_version(frame_payload(frames[2])),
                "score": 0.5,
                "reasons": [],
            },
        ],
    )
    revision = workspace.revision
    assert label_check.apply(workspace, store) == 1
    assert label_check.apply(workspace, store) == 0
    flagged = Frame.objects.get(pk=frames[0].pk)
    assert flagged.metadata["label_check"]["score"] == pytest.approx(SCORE)
    assert frame_version(frame_payload(flagged)) == version
    assert Workspace.objects.get().revision == revision + 1

    client = verified(owner)
    state = client.get(f"/video-analysis/state?scope=recording&match={match_id}").json()
    shown = {
        f["id"]: f
        for f in state["matches"][0]["frames"]
        if f["id"] in {frames[0].source_id, frames[1].source_id}
    }
    assert shown[frames[0].source_id]["label_check"]["reasons"] == reasons
    assert "label_check" not in shown[frames[1].source_id]

    response = client.post(
        "/video-analysis/review",
        json.dumps({
            "action": "review",
            "match_id": match_id,
            "frame_id": frames[0].source_id,
            "expected_frame_version": version,
            "status": "approved",
            "annotation": labels,
            "complete": True,
        }),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=state["csrf"],
    )
    assert response.status_code == HTTPStatus.OK, response.json()
    assert "label_check" not in Frame.objects.get(pk=frames[0].pk).metadata
    # The same ranking cannot bring back a flag for work that was checked.
    assert label_check.apply(workspace, store) == 0

    # A newer run replaces older flags.
    write_report(store, [], run="remote-b")
    approve(Frame.objects.get(pk=frames[2].pk), labels)
    frame = Frame.objects.get(pk=frames[2].pk)
    frame.metadata = {**frame.metadata, "label_check": {"run": "remote-a"}}
    frame.save(update_fields=["metadata"])
    assert label_check.apply(workspace, store) == 1
    assert "label_check" not in Frame.objects.get(pk=frames[2].pk).metadata


def test_mixed_queue_balances_recordings_in_same_match_batches(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Least-covered recordings go first, a batch at a time, flagged work leading."""
    owner, _, _ = imported
    workspace = Workspace.objects.get()
    covered = Recording.objects.get()
    labels = annotation(box("player", [0.1, 0.1, 0.1, 0.3], confidence=1.0))
    first = Frame.objects.filter(recording=covered).order_by("position").first()
    assert first is not None
    approve(first, labels)
    first.metadata = {
        **first.metadata,
        "label_check": {
            "run": "r",
            "score": 2.0,
            "reasons": [],
            "frame_version": frame_version(frame_payload(first)),
        },
    }
    first.save(update_fields=["metadata"])
    fresh = Recording.objects.create(
        workspace=workspace, source_id="fresh", metadata={"title": "Fresh"}
    )
    quiet = annotation(box("player", [0.1, 0.1, 0.1, 0.3], confidence=0.9))
    doubtful = annotation(
        box("player", [0.1, 0.1, 0.1, 0.3], confidence=0.4),
        box("player", [0.15, 0.1, 0.1, 0.3], confidence=0.9),
    )
    for i in range(QUEUE_BATCH + 2):
        Frame.objects.create(
            recording=fresh,
            source_id=f"fresh-{i}",
            position=i,
            metadata={"time_seconds": i, "image": f"fresh/{i}.jpg"},
            proposal=doubtful if i == DOUBTFUL else quiet if i else None,
        )
    queue = verified(owner).get("/video-analysis/queue").json()
    items = queue["items"]
    batch = items[:QUEUE_BATCH]
    assert {item["match_id"] for item in batch} == {"fresh"}
    # Doubt and crowding first; frames without a draft last.
    assert batch[0]["frame_id"] == f"fresh-{DOUBTFUL}"
    assert "fresh-0" not in {item["frame_id"] for item in batch}
    covered_batch = items[QUEUE_BATCH : 2 * QUEUE_BATCH]
    assert covered_batch[0] == {
        "match_id": covered.source_id,
        "frame_id": first.source_id,
        "kind": "check",
        "time_seconds": first.metadata.get("time_seconds", 0),
    }
    assert {i["frame_id"] for i in items} >= {"fresh-0", first.source_id}
