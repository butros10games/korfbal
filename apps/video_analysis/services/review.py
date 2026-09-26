"""Short, durable interactive review writes without exporting the workspace."""

from typing import Any

from django.contrib.auth.models import User
from django.db import transaction

from apps.kwt_common.services.jobs import enqueue
from apps.video_analysis.engine.store import ConflictError, Store, frame_version
from apps.video_analysis.engine.timeline import validate_periods
from apps.video_analysis.models import Frame, Recording, ReviewAudit, Workspace
from apps.video_analysis.queries import frame_payload
from apps.video_analysis.services import blind_check


def save(workspace: Workspace, actor: User, payload: dict[str, Any]) -> dict:
    """Dispatch a scoped review or recording timing write.

    Raises:
        ValueError: The requested operation is unknown.

    """
    if payload.get("action") == "timing":
        return timing(workspace, payload)
    if payload.get("action") == "review":
        return save_frame(workspace, actor, payload)
    if payload.get("action") == "swap_teams":
        return swap_draft_teams(workspace, payload)
    if payload.get("action") == "blind":
        return blind_check.save(workspace, actor, payload)
    raise ValueError("Unknown review action")


@transaction.atomic
def save_frame(workspace: Workspace, actor: User, payload: dict[str, Any]) -> dict:
    """Validate and save one frame, retaining export intent in the same transaction.

    Raises:
        ValueError: The frame is unknown or removed.
        ConflictError: The caller's frame or workspace revision is stale.

    """
    locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
    frame = Frame.objects.filter(
        recording__workspace=workspace,
        recording__source_id=payload.get("match_id"),
        source_id=payload.get("frame_id"),
    ).first()
    if frame is None or frame.metadata.get("dataset_decision") == "removed":
        raise ValueError("Unknown frame")
    raw = frame_payload(frame)
    if (
        payload["expected_frame_version"] != frame_version(raw)
        if "expected_frame_version" in payload
        else payload.get("revision") != locked.revision
    ):
        raise ConflictError(
            "Another review was saved. Reload before applying your edit."
        )
    Store._review({"frames": [raw]}, payload)
    frame.metadata = {
        **{
            k: v
            for k, v in frame.metadata.items()
            if k not in {"annotation_provenance", "label_check"}
        },
        "reviewed_at": raw["reviewed_at"],
    }
    for key in ("correction", "history", "status", "complete"):
        setattr(frame, key, raw[key])
    frame.save(
        update_fields=["metadata", "correction", "history", "status", "complete"]
    )
    locked.revision += 1
    locked.save(update_fields=["revision"])
    ReviewAudit.objects.create(
        frame=frame, actor=actor, revision=locked.revision, payload=raw
    )
    publish_later(workspace)
    return {
        "revision": locked.revision,
        "frame": dict(raw, frame_version=frame_version(raw)),
    }


def publish_later(workspace: Workspace) -> None:
    """Coalesce exports without losing edits that arrive during publication."""
    enqueue(
        "apps.video_analysis.tasks.publish_reviews",
        str(workspace.pk),
        args=[str(workspace.pk)],
        queue="vision",
    )


@transaction.atomic
def timing(workspace: Workspace, payload: dict[str, Any]) -> dict:
    """Change one recording's timing while preserving the workspace revision check.

    Raises:
        ConflictError: The workspace changed since the timing editor was opened.
        ValueError: The recording is unknown.

    """
    locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
    if payload.get("revision") != locked.revision:
        raise ConflictError(
            "Another review was saved. Reload before applying your edit."
        )
    recording = Recording.objects.filter(
        workspace=workspace, source_id=payload.get("match_id")
    ).first()
    if recording is None:
        raise ValueError("Unknown match")
    Store._timing(recording.metadata, payload)
    recording.save(update_fields=["metadata"])
    locked.revision += 1
    locked.save(update_fields=["revision"])
    publish_later(workspace)
    return {"revision": locked.revision}


SWAPPED_TEAMS = {"team_a": "team_b", "team_b": "team_a"}


@transaction.atomic
def swap_draft_teams(workspace: Workspace, payload: dict[str, Any]) -> dict:
    """Swap club A and B on every open model draft of one recording.

    Shirt colours group players consistently within a match, but which group is
    the first club can be wrong. Human corrections and reviewed frames are never
    changed: they already say which club each player belongs to.

    Raises:
        ConflictError: The workspace changed since the reviewer loaded it.
        ValueError: The recording is unknown.

    """
    locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
    if payload.get("revision") != locked.revision:
        raise ConflictError(
            "Another review was saved. Reload before applying your edit."
        )
    recording = Recording.objects.filter(
        workspace=workspace, source_id=payload.get("match_id")
    ).first()
    if recording is None:
        raise ValueError("Unknown match")
    swapped = 0
    for frame in Frame.objects.select_for_update().filter(
        recording=recording,
        status="pending",
        correction__isnull=True,
        proposal__isnull=False,
    ):
        objects = frame.proposal.get("objects", [])
        if not any(obj.get("team") in SWAPPED_TEAMS for obj in objects):
            continue
        frame.proposal = {
            **frame.proposal,
            "objects": [
                {**obj, "team": SWAPPED_TEAMS.get(obj.get("team"), obj.get("team"))}
                if obj.get("label") == "player"
                else obj
                for obj in objects
            ],
        }
        frame.save(update_fields=["proposal"])
        swapped += 1
    locked.revision += 1
    locked.save(update_fields=["revision"])
    publish_later(workspace)
    return {"revision": locked.revision, "swapped": swapped}


@transaction.atomic
def timeline(workspace: Workspace, payload: dict[str, Any]) -> dict:
    """Save source-video active periods with optimistic workspace revisioning.

    Raises:
        ConflictError: The workspace revision changed since the editor loaded.
        ValueError: The recording or its active periods are invalid.

    """
    locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
    if payload.get("revision") != locked.revision:
        raise ConflictError("Timeline changed. Reload before saving your edit.")
    recording = Recording.objects.filter(
        workspace=workspace, source_id=payload.get("match_id")
    ).first()
    if recording is None or not recording.metadata.get("video"):
        raise ValueError("Choose a recording with video")
    periods = validate_periods(
        payload.get("active_periods"), recording.metadata["duration_seconds"]
    )
    metadata = {**recording.metadata, "active_periods": periods}
    if not metadata.get("timing_history"):
        metadata["match_start_seconds"] = (
            metadata.get("source_offset_seconds", 0) + periods[0]["start"]
        )
    recording.metadata = metadata
    recording.save(update_fields=["metadata"])
    locked.revision += 1
    locked.save(update_fields=["revision"])
    publish_later(workspace)
    return {"revision": locked.revision, "active_periods": periods}
