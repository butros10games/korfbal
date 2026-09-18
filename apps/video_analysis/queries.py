"""Narrow read queries for the native review API."""

from typing import Any

from django.db.models import Count, Q

from apps.video_analysis.engine.store import frame_version
from apps.video_analysis.models import Frame, Recording, Workspace


def frame_payload(frame: Frame) -> dict[str, Any]:
    """Keep dataset membership separate from the annotation fingerprint."""
    metadata = {
        k: v
        for k, v in frame.metadata.items()
        if k not in {"dataset_decision", "dataset_revision"}
    }
    return dict(
        metadata,
        id=frame.source_id,
        proposal=frame.proposal,
        correction=frame.correction,
        history=frame.history,
        status=frame.status,
        complete=frame.complete,
    )


def registered_media(workspace: Workspace, relative: str) -> bool:
    """Authorize registered media without reading or locking annotation state."""
    return bool(relative) and (
        Frame.objects.filter(
            recording__workspace=workspace, metadata__image=relative
        ).exists()
        or Recording.objects.filter(
            workspace=workspace, metadata__video=relative
        ).exists()
    )


def review_state(workspace: Workspace, match_id: str) -> dict[str, Any]:
    """Load annotations for one recording and only navigation metadata for others."""
    active = Q(metadata__dataset_decision__isnull=True) | ~Q(
        metadata__dataset_decision="removed"
    )
    recordings = list(
        Recording.objects
        .filter(workspace=workspace)
        .annotate(
            frame_count=Count(
                "frames",
                filter=Q(frames__metadata__dataset_decision__isnull=True)
                | ~Q(frames__metadata__dataset_decision="removed"),
            )
        )
        .values("pk", "source_id", "metadata", "frame_count")
    )
    selected = next(
        (r for r in recordings if r["source_id"] == match_id and r["frame_count"]), None
    )
    if selected is None:
        selected = next((r for r in recordings if r["frame_count"]), None)
    frames = []
    if selected:
        for frame in Frame.objects.filter(recording_id=selected["pk"]).filter(active):
            raw = frame_payload(frame)
            frames.append(dict(raw, frame_version=frame_version(raw)))
    return {
        "schema_version": 1,
        "revision": workspace.revision,
        "matches": [
            dict(
                r["metadata"],
                id=r["source_id"],
                frame_count=r["frame_count"],
                frames=frames if r == selected else [],
            )
            for r in recordings
        ],
    }
