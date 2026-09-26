"""Reversible image membership, independent of annotation approval."""

from typing import Any

from django.contrib.auth.models import User
from django.db import transaction

from apps.video_analysis.engine.store import ConflictError, frame_version
from apps.video_analysis.models import Frame, Recording, ReviewAudit, Workspace
from apps.video_analysis.queries import frame_payload
from apps.video_analysis.services.review import publish_later


DECISIONS = {"unreviewed", "kept", "removed"}
PAGE_SIZE = 24


def describe(frame: Frame) -> dict[str, Any]:
    """Return only the image and concurrency fields needed for triage."""
    return {
        "id": frame.pk,
        "match_id": frame.recording.source_id,
        "frame_id": frame.source_id,
        "title": frame.recording.metadata.get("title", frame.recording.source_id),
        "image": frame.metadata["image"],
        "time_seconds": frame.metadata.get(
            "source_time_seconds", frame.metadata.get("time_seconds", 0)
        ),
        "decision": frame.metadata.get("dataset_decision", "unreviewed"),
        "dataset_revision": frame.metadata.get("dataset_revision", 0),
        "frame_version": frame_version(frame_payload(frame)),
    }


def listing(
    workspace: Workspace, decision: str, after: int = 0, recording: str = ""
) -> dict[str, Any]:
    """Read a bounded image page and membership counts.

    Raises:
        ValueError: The filter or cursor is invalid.

    """
    if decision not in DECISIONS or after < 0:
        raise ValueError("Invalid dataset filter")
    all_frames = Frame.objects.filter(recording__workspace=workspace)
    if recording:
        all_frames = all_frames.filter(recording__source_id=recording)
    kept = all_frames.filter(metadata__dataset_decision="kept").count()
    removed = all_frames.filter(metadata__dataset_decision="removed").count()
    total = all_frames.count()
    selected = (
        all_frames.filter(metadata__dataset_decision__isnull=True)
        if decision == "unreviewed"
        else all_frames.filter(metadata__dataset_decision=decision)
    )
    frames = list(
        selected
        .filter(pk__gt=after)
        .select_related("recording")
        .order_by("pk")[: PAGE_SIZE + 1]
    )
    page = frames[:PAGE_SIZE]
    return {
        "recordings": [
            {"id": r.source_id, "title": r.metadata.get("title", r.source_id)}
            for r in Recording.objects.filter(workspace=workspace)
        ],
        "frames": [describe(frame) for frame in page],
        "next": page[-1].pk if len(frames) > PAGE_SIZE else None,
        "counts": {
            "unreviewed": total - kept - removed,
            "kept": kept,
            "removed": removed,
            "active": total - removed,
        },
    }


@transaction.atomic
def decide(
    workspace: Workspace, actor: User, payload: dict[str, Any]
) -> dict[str, Any]:
    """Change membership under the workspace lock and retain an undoable audit.

    Raises:
        ValueError: The decision or frame is invalid.
        ConflictError: Membership or annotations changed since the image was opened.

    """
    decision = payload.get("decision")
    if decision not in DECISIONS:
        raise ValueError("Choose Keep or Remove from dataset")
    locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
    frame = (
        Frame.objects
        .select_for_update(of=("self",))
        .select_related("recording")
        .filter(pk=payload.get("id"), recording__workspace=workspace)
        .first()
    )
    if frame is None:
        raise ValueError("Unknown dataset image")
    revision = frame.metadata.get("dataset_revision", 0)
    if payload.get("dataset_revision") != revision or payload.get(
        "frame_version"
    ) != frame_version(frame_payload(frame)):
        raise ConflictError("This image changed. Refresh before deciding.")
    previous = frame.metadata.get("dataset_decision", "unreviewed")
    metadata = dict(frame.metadata, dataset_revision=revision + 1)
    if decision == "unreviewed":
        metadata.pop("dataset_decision", None)
    else:
        metadata["dataset_decision"] = decision
    frame.metadata = metadata
    frame.save(update_fields=["metadata"])
    locked.revision += 1
    locked.save(update_fields=["revision"])
    ReviewAudit.objects.create(
        frame=frame,
        actor=actor,
        revision=locked.revision,
        payload={"action": "dataset", "previous": previous, "decision": decision},
    )
    publish_later(workspace)
    return {"frame": describe(frame), "previous": previous}
