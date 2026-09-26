"""Blind re-labelling of AI-approved frames, kept apart from the approved labels."""

from http import HTTPStatus
import json
from typing import Any

from django.contrib.auth.models import User
from django.core.management import call_command
import pytest

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.store import Store, frame_version
from apps.video_analysis.models import Frame, Recording, Workspace
from apps.video_analysis.queries import frame_payload
from apps.video_analysis.services import blind_check

from .test_review import verified


pytestmark = pytest.mark.django_db


def labels(*objects: dict[str, Any]) -> dict[str, Any]:
    """Build a reviewed live annotation."""
    return {"scene": "live", "event": "none", "notes": "", "objects": list(objects)}


def player(bbox: list[float], label: str = "player") -> dict[str, Any]:
    """One confident box."""
    return {"label": label, "bbox": bbox, "confidence": 1.0}


def ai_approve(frame: Frame, annotation: dict[str, Any]) -> None:
    """Record labels that came from the AI reviewer."""
    frame.correction = annotation
    frame.status = "approved"
    frame.complete = True
    frame.metadata = {**frame.metadata, "annotation_provenance": {"kind": "ai"}}
    frame.save()


def test_blind_labels_are_stored_apart_and_compared(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The approved labels and fingerprint never change; the report counts errors."""
    owner, _, _ = imported
    workspace = Workspace.objects.get()
    other = Recording.objects.create(workspace=workspace, source_id="other")
    frames = list(Frame.objects.order_by("position")[:2])
    frames.append(Frame.objects.create(recording=other, source_id="o0", position=0))
    ai = labels(player([0.1, 0.1, 0.1, 0.3]), player([0.5, 0.1, 0.1, 0.3]))
    for frame in frames:
        ai_approve(frame, ai)
    assert blind_check.create_sample(workspace, "s1", size=2) == 2  # noqa: PLR2004
    marked = Frame.objects.filter(metadata__blind_check__status="open")
    # Spread over recordings before taking a second frame from one.
    assert {f.recording.source_id for f in marked} == {
        frames[0].recording.source_id,
        "other",
    }
    target = marked.select_related("recording").first()
    assert target is not None
    version = frame_version(frame_payload(target))
    client = verified(owner)
    state = client.get("/video-analysis/state").json()
    queue = client.get("/video-analysis/queue?kind=blind").json()["items"]
    assert len(queue) == 2  # noqa: PLR2004
    blind = labels(
        player([0.1, 0.1, 0.1, 0.3]),  # agrees
        player([0.5, 0.1, 0.1, 0.3], "referee"),  # the AI called this a player
        player([0.8, 0.1, 0.1, 0.3]),  # the AI missed this one
    )
    response = client.post(
        "/video-analysis/review",
        json.dumps({
            "action": "blind",
            "match_id": target.recording.source_id,
            "frame_id": target.source_id,
            "annotation": blind,
        }),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=state["csrf"],
    )
    assert response.status_code == HTTPStatus.OK
    target.refresh_from_db()
    assert target.correction == ai
    assert frame_version(frame_payload(target)) == version
    assert len(client.get("/video-analysis/queue?kind=blind").json()["items"]) == 1
    report = blind_check.report(workspace, "s1")
    assert report["checked"] == 1
    assert report["labels"]["player"] == {
        "blind_boxes": 2,
        "ai_missed": 1,
        "ai_extra": 0,
        "role_disagreements": 0,
        "mean_iou": 1.0,
    }
    assert report["labels"]["referee"]["role_disagreements"] == 1
    # A second save is refused: the check is done.
    again = client.post(
        "/video-analysis/review",
        json.dumps({
            "action": "blind",
            "match_id": target.recording.source_id,
            "frame_id": target.source_id,
            "annotation": blind,
        }),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=state["csrf"],
    )
    assert again.status_code == HTTPStatus.BAD_REQUEST
    call_command("blind_check", "report", "s1")


def test_review_state_shows_only_the_blind_check_status(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The editor learns a frame is blind without seeing stored blind labels."""
    owner, _, _ = imported
    frame = Frame.objects.select_related("recording").order_by("position").first()
    assert frame is not None
    ai_approve(frame, labels(player([0.1, 0.1, 0.1, 0.3])))
    frame.metadata = {
        **frame.metadata,
        "blind_check": {"status": "done", "sample": "s", "annotation": labels()},
    }
    frame.save(update_fields=["metadata"])
    state = (
        verified(owner)
        .get(f"/video-analysis/state?scope=recording&match={frame.recording.source_id}")
        .json()
    )
    shown = next(f for f in state["matches"][0]["frames"] if f["id"] == frame.source_id)
    assert shown["blind_check"] == {"status": "done"}
