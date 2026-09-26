"""Send approved frames the latest trained model disagrees with back for a check.

Flags live in frame metadata but outside the annotation fingerprint, so setting
one never conflicts with an open editor. A flag only counts while the frame is
unchanged since training; saving the frame again clears it for good.
"""

import json
from typing import Any

from django.db import transaction
from django.db.models import Q

from apps.video_analysis.engine.label_check import FLAG_SCORE
from apps.video_analysis.engine.store import Store, frame_version
from apps.video_analysis.models import Frame, Workspace
from apps.video_analysis.queries import frame_payload
from apps.video_analysis.services.review import publish_later


# A reviewable batch; the worst disagreements first.
MAX_FLAGS = 100


def latest_report(store: Store) -> tuple[str, dict[str, Any]] | None:
    """Newest completed training run that returned a label ranking."""
    runs = []
    for path in (store.root / "vision/runs").glob("*/label_check.json"):
        run = json.loads((path.parent / "run.json").read_text())
        if run.get("kind") == "train" and run.get("status") == "completed":
            runs.append((run.get("created_at", ""), run["id"], path))
    if not runs:
        return None
    _, run_id, path = max(runs)
    return run_id, json.loads(path.read_text())


def current_flag(frame: Frame) -> dict[str, Any] | None:
    """Return the flag for exactly this frame version; None once saved again."""
    flag = frame.metadata.get("label_check")
    if (
        flag
        and frame.status == "approved"
        and flag.get("frame_version") == frame_version(frame_payload(frame))
    ):
        return flag
    return None


def apply(workspace: Workspace, store: Store) -> int:
    """Mirror the newest ranking into frame flags; returns the number changed."""
    found = latest_report(store)
    if found is None:
        return 0
    run_id, report = found
    wanted = {
        (item["match_id"], item["frame_id"]): item
        for item in report["frames"][:MAX_FLAGS]
        if item["score"] >= FLAG_SCORE
    }
    identities = Q(pk__in=[])
    for key in wanted:
        identities |= Q(recording__source_id=key[0], source_id=key[1])
    candidates = Q(metadata__label_check__isnull=False) | identities
    with transaction.atomic():
        locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
        changed = 0
        for frame in (
            Frame.objects
            .select_for_update(of=("self",))
            .select_related("recording")
            .filter(recording__workspace=workspace)
            .filter(candidates)
        ):
            item = wanted.get((frame.recording.source_id, frame.source_id))
            flag = frame.metadata.get("label_check")
            version = frame_version(frame_payload(frame))
            desired = None
            if (
                item is not None
                and frame.status == "approved"
                and frame.metadata.get("dataset_decision") != "removed"
                and item["frame_version"] == version
            ):
                desired = {
                    "run": run_id,
                    "frame_version": version,
                    "score": item["score"],
                    "reasons": item["reasons"],
                }
            if flag == desired:
                continue
            metadata = {k: v for k, v in frame.metadata.items() if k != "label_check"}
            frame.metadata = (
                {**metadata, "label_check": desired} if desired else metadata
            )
            frame.save(update_fields=["metadata"])
            changed += 1
        if changed:
            locked.revision += 1
            locked.save(update_fields=["revision"])
            publish_later(workspace)
    return changed
