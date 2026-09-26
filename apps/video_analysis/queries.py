"""Narrow read queries for the native review API."""

from operator import itemgetter
from typing import Any

from django.db.models import Count, Q

from apps.video_analysis.engine.store import frame_version
from apps.video_analysis.models import Frame, Recording, Workspace


def frame_payload(frame: Frame) -> dict[str, Any]:
    """Keep dataset membership and label-check flags out of the fingerprint."""
    metadata = {
        k: v
        for k, v in frame.metadata.items()
        if k
        not in {"dataset_decision", "dataset_revision", "label_check", "blind_check"}
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
        for frame in (
            Frame.objects
            .filter(recording_id=selected["pk"])
            .filter(active)
            .order_by("metadata__time_seconds", "pk")
        ):
            raw = frame_payload(frame)
            version = frame_version(raw)
            flag = frame.metadata.get("label_check")
            frames.append(
                dict(
                    raw,
                    frame_version=version,
                    # A flag lapses as soon as the frame is saved again.
                    **(
                        {"label_check": flag}
                        if flag
                        and frame.status == "approved"
                        and flag.get("frame_version") == version
                        else {}
                    ),
                    # Only the status: the blind labels stay out of the editor.
                    **(
                        {
                            "blind_check": {
                                "status": frame.metadata["blind_check"].get("status")
                            }
                        }
                        if frame.metadata.get("blind_check")
                        else {}
                    ),
                )
            )
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


# Frames per match before switching: one hall, lighting and kit at a time.
QUEUE_BATCH = 8
QUEUE_BATCHES = 6
# Approved frames after which a recording adds little new variety: beyond it the
# mixed queue only serves its label checks and doubtful drafts.
MATCH_FRAME_CAP = 100
PEOPLE = frozenset({"player", "referee"})
# Matches reviewWorkflow.crowding: 20% of the smaller box hides a limb.
MIN_OVERLAP = 0.2
# Model confidences it cannot decide on; bounded by the proposal threshold.
UNSURE = (0.25, 0.6)


def overlap(a: list[float], b: list[float]) -> float:
    """Share of the smaller box covered by the other."""
    width = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    height = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    return width * height / max(1e-9, min(a[2] * a[3], b[2] * b[3]))


def open_priority(proposal: dict[str, Any] | None) -> tuple[int, int, int]:
    """Drafted, doubtful and crowded open frames teach the model most per minute."""
    objects = (proposal or {}).get("objects", [])
    unsure = sum(UNSURE[0] <= o.get("confidence", 1) < UNSURE[1] for o in objects)
    boxes = [o["bbox"] for o in objects if o["label"] in PEOPLE]
    crowded = sum(
        any(
            j != i and overlap(box, other) >= MIN_OVERLAP
            for j, other in enumerate(boxes)
        )
        for i, box in enumerate(boxes)
    )
    return int(proposal is not None), unsure, crowded


def review_queue(workspace: Workspace) -> dict[str, Any]:
    """Mix recordings in short same-match batches, least-covered recordings first.

    Approved frames the latest model disagrees with come first in their batch,
    then open frames by draft, model doubt and crowding. Recordings with
    MATCH_FRAME_CAP approved frames only offer checks and doubtful drafts. Each
    call re-plans from current counts, so finishing a batch moves to another match.
    """
    active = Q(metadata__dataset_decision__isnull=True) | ~Q(
        metadata__dataset_decision="removed"
    )
    rows = (
        Frame.objects
        .filter(recording__workspace=workspace)
        .filter(active)
        .filter(Q(status="pending") | Q(metadata__label_check__isnull=False))
        .select_related("recording")
        .only(
            "source_id",
            "metadata",
            "proposal",
            "correction",
            "history",
            "status",
            "complete",
            "recording__source_id",
            "recording__metadata",
        )
    )
    candidates: dict[str, list[tuple[tuple, dict]]] = {}
    for frame in rows.iterator(chunk_size=500):
        match_id = frame.recording.source_id
        if frame.status == "pending":
            key = (0, *(-v for v in open_priority(frame.proposal)))
            kind = "open"
        else:
            raw = frame_payload(frame)
            flag = frame.metadata.get("label_check") or {}
            if flag.get("frame_version") != frame_version(raw):
                continue
            key = (-1, -flag.get("score", 0), 0, 0)
            kind = "check"
        item = {
            "match_id": match_id,
            "frame_id": frame.source_id,
            "kind": kind,
            "time_seconds": frame.metadata.get("time_seconds", 0),
        }
        candidates.setdefault(match_id, []).append(((*key, item["time_seconds"]), item))
    approved = dict(
        Recording.objects
        .filter(workspace=workspace, source_id__in=candidates)
        .annotate(
            done=Count(
                "frames",
                filter=Q(frames__status="approved")
                & (
                    Q(frames__metadata__dataset_decision__isnull=True)
                    | ~Q(frames__metadata__dataset_decision="removed")
                ),
            )
        )
        .values_list("source_id", "done")
    )
    for match_id, items in candidates.items():
        if approved.get(match_id, 0) >= MATCH_FRAME_CAP:
            # key[2] is minus the draft's doubtful-box count; checks lead with -1.
            items[:] = [e for e in items if e[1]["kind"] == "check" or e[0][2] < 0]
        items.sort(key=itemgetter(0))
    planned = dict.fromkeys(candidates, 0)
    queue = []
    for _ in range(QUEUE_BATCHES):
        open_matches = [m for m, items in candidates.items() if items]
        if not open_matches:
            break
        # Balance the dataset: the match with the fewest (planned) labels goes next.
        match_id = min(open_matches, key=lambda m: (approved.get(m, 0) + planned[m], m))
        batch = candidates[match_id][:QUEUE_BATCH]
        del candidates[match_id][:QUEUE_BATCH]
        planned[match_id] += len(batch)
        queue.extend(item for _, item in batch)
    return {
        "items": queue,
        "remaining": sum(len(items) for items in candidates.values()),
        "approved": approved,
    }


def check_queue(workspace: Workspace) -> dict[str, Any]:
    """Every approved frame still flagged by a label check, across recordings.

    Frames stay grouped per recording (one hall and kit at a time); recordings
    with the most open checks come first, and each recording's worst frame first.
    """
    per_match: dict[str, list[tuple[float, dict]]] = {}
    for frame in Frame.objects.filter(
        Q(metadata__dataset_decision__isnull=True)
        | ~Q(metadata__dataset_decision="removed"),
        recording__workspace=workspace,
        status="approved",
        metadata__label_check__isnull=False,
    ).select_related("recording"):
        flag = frame.metadata["label_check"]
        if flag.get("frame_version") != frame_version(frame_payload(frame)):
            continue
        match_id = frame.recording.source_id
        per_match.setdefault(match_id, []).append((
            flag.get("score", 0),
            {
                "match_id": match_id,
                "frame_id": frame.source_id,
                "kind": "check",
                "time_seconds": frame.metadata.get("time_seconds", 0),
            },
        ))
    items = []
    for match_id in sorted(per_match, key=lambda m: (-len(per_match[m]), m)):
        items.extend(
            item for _, item in sorted(per_match[match_id], key=lambda e: -e[0])
        )
    return {"items": items, "remaining": 0}


def blind_queue(workspace: Workspace) -> dict[str, Any]:
    """Frames still waiting for a blind check, grouped per recording."""
    items = [
        {
            "match_id": frame.recording.source_id,
            "frame_id": frame.source_id,
            "kind": "blind",
            "time_seconds": frame.metadata.get("time_seconds", 0),
        }
        for frame in Frame.objects
        .filter(
            recording__workspace=workspace,
            metadata__blind_check__status="open",
        )
        .select_related("recording")
        .order_by("recording__source_id", "position")
    ]
    return {"items": items, "remaining": 0}
