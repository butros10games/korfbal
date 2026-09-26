"""Measure AI-approved labels against a reviewer's blind re-labelling.

A sample of AI-approved frames is marked for a blind check. The reviewer labels
each from an empty canvas; the result is stored beside the frame and never
replaces its approved correction. The report compares the two.
"""

from collections import defaultdict
import random
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from apps.video_analysis.engine.boxes import iou
from apps.video_analysis.engine.store import validate_annotation
from apps.video_analysis.models import Frame, ReviewAudit, Workspace


SAMPLE_SIZE = 40
LABELS = ("player", "referee", "basket", "ball")


def ai_approved(workspace: Workspace) -> list[Frame]:
    """Approved frames whose labels still come from the AI reviewer."""
    return [
        frame
        for frame in Frame.objects
        .filter(recording__workspace=workspace, status="approved")
        .exclude(correction__isnull=True)
        .select_related("recording")
        .order_by("pk")
        if (frame.metadata.get("annotation_provenance") or {}).get("kind") == "ai"
        and frame.metadata.get("dataset_decision") != "removed"
        and "blind_check" not in frame.metadata
    ]


@transaction.atomic
def create_sample(
    workspace: Workspace, name: str, size: int = SAMPLE_SIZE, seed: int = 0
) -> int:
    """Mark an evenly spread sample: one frame per recording in turn.

    Returns:
        The number of frames marked for a blind check.

    """
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    per_recording: dict[str, list[Frame]] = defaultdict(list)
    for frame in ai_approved(workspace):
        per_recording[frame.recording.source_id].append(frame)
    rng = random.Random(seed)  # noqa: S311 - sampling, not security
    for frames in per_recording.values():
        rng.shuffle(frames)
    chosen: list[Frame] = []
    while len(chosen) < size and any(per_recording.values()):
        for match_id in sorted(per_recording):
            if per_recording[match_id] and len(chosen) < size:
                chosen.append(per_recording[match_id].pop())
    for frame in chosen:
        frame.metadata = {
            **frame.metadata,
            "blind_check": {"status": "open", "sample": name},
        }
        frame.save(update_fields=["metadata"])
    return len(chosen)


@transaction.atomic
def save(workspace: Workspace, actor: User, payload: dict[str, Any]) -> dict:
    """Store a blind annotation beside the frame without touching its labels.

    Raises:
        ValueError: The frame is not open for a blind check.

    """
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    frame = (
        Frame.objects
        .select_for_update()
        .filter(
            recording__workspace=workspace,
            recording__source_id=payload.get("match_id"),
            source_id=payload.get("frame_id"),
        )
        .first()
    )
    check = (frame.metadata.get("blind_check") or {}) if frame else {}
    if frame is None or check.get("status") != "open":
        raise ValueError("This frame is not waiting for a blind check")
    done = {
        **check,
        "status": "done",
        "annotation": validate_annotation(payload["annotation"]),
        "saved_at": timezone.now().isoformat(),
    }
    frame.metadata = {**frame.metadata, "blind_check": done}
    frame.save(update_fields=["metadata"])
    ReviewAudit.objects.create(
        frame=frame,
        actor=actor,
        revision=workspace.revision,
        payload={"action": "blind_check", **done},
    )
    return {"blind_check": {"status": "done", "sample": check.get("sample")}}


def compare(reference: dict[str, Any], blind: dict[str, Any]) -> dict[str, int]:
    """Count agreements between the AI labels and the blind labels of one frame."""
    counts: dict[str, int] = defaultdict(int)
    reference_objects = [o for o in reference["objects"] if o["label"] in LABELS]
    blind_objects = [o for o in blind["objects"] if o["label"] in LABELS]
    used: set[int] = set()
    for obj in blind_objects:
        counts[f"blind_{obj['label']}"] += 1
        threshold = 0.3 if obj["label"] == "ball" else 0.5
        scored = [
            (iou(obj["bbox"], ref["bbox"]), j)
            for j, ref in enumerate(reference_objects)
            if j not in used
        ]
        best = max(scored, default=(0.0, -1))
        if best[0] < threshold:
            counts[f"missed_by_ai_{obj['label']}"] += 1
            continue
        used.add(best[1])
        ref = reference_objects[best[1]]
        if ref["label"] != obj["label"]:
            counts[f"role_{obj['label']}_as_{ref['label']}"] += 1
        else:
            counts[f"matched_{obj['label']}"] += 1
            counts[f"iou_sum_{obj['label']}"] += round(best[0] * 1000)
    for j, ref in enumerate(reference_objects):
        if j not in used:
            counts[f"extra_in_ai_{ref['label']}"] += 1
    return dict(counts)


def report(workspace: Workspace, name: str) -> dict[str, Any]:
    """Summarise every finished blind check of one sample."""
    totals: dict[str, int] = defaultdict(int)
    frames = done = 0
    for frame in Frame.objects.filter(
        recording__workspace=workspace, metadata__blind_check__sample=name
    ):
        frames += 1
        check = frame.metadata["blind_check"]
        if check.get("status") != "done" or not frame.correction:
            continue
        done += 1
        for key, value in compare(frame.correction, check["annotation"]).items():
            totals[key] += value
    per_label = {}
    for label in LABELS:
        blind = totals[f"blind_{label}"]
        matched = totals[f"matched_{label}"]
        per_label[label] = {
            "blind_boxes": blind,
            "ai_missed": totals[f"missed_by_ai_{label}"],
            "ai_extra": totals[f"extra_in_ai_{label}"],
            "role_disagreements": sum(
                v for k, v in totals.items() if k.startswith(f"role_{label}_as_")
            ),
            "mean_iou": round(totals[f"iou_sum_{label}"] / 1000 / matched, 3)
            if matched
            else None,
        }
    return {"sample": name, "frames": frames, "checked": done, "labels": per_label}
