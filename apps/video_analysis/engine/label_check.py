"""Rank approved frames by how strongly a freshly trained model disagrees.

The model has just been fitted to these labels, so a confident disagreement on
its own training data usually means a missing, loose or mislabelled box. Reasons
are hints for a reviewer, never automatic corrections.
"""

from __future__ import annotations

from collections.abc import Callable
import math
from pathlib import Path
from typing import Any

from .boxes import iou


VERSION = 1
# Low enough to notice a box the model half-sees; reasons apply their own bars.
PREDICT_CONFIDENCE = 0.1
MATCH_IOU = 0.5
LOOSE_IOU = 0.3
MISSING_CONFIDENCE = 0.6
ROLE_CONFIDENCE = 0.5
# Normalized image distance; ~40 px on a 1280 px wide frame.
FOOT_DISTANCE = 0.03
FLAG_SCORE = 1.0
MAX_REASONS = 12
# A confident (>= 2/3) unlabelled person or role disagreement flags a frame alone.
WEIGHTS = {
    "unlabelled": 1.5,
    "wrong_role": 1.5,
    "unseen_label": 1.0,
    "loose_box": 0.5,
    "missing_post_foot": 0.5,
    "post_foot_off": 0.5,
}
CHECKED = frozenset({"player", "referee", "basket", "ball"})
ROLES = frozenset({"player", "referee"})


def disagreements(
    prediction: dict[str, Any],
    reference: dict[str, Any],
    classes: list[str],
    *,
    feet: bool = False,
) -> list[dict[str, Any]]:
    """List where the model and the approved labels disagree, most useful first."""
    labels = CHECKED & set(classes)
    predicted = sorted(
        (o for o in prediction["objects"] if o["label"] in labels),
        key=lambda o: -o.get("confidence", 0),
    )
    approved = [o for o in reference["objects"] if o["label"] in labels]
    pairs, used, reasons = match_predictions(predicted, approved)
    for j, ref in enumerate(approved):
        if j in used:
            continue
        closest = max(
            (
                iou(p["bbox"], ref["bbox"])
                for p in predicted
                if p["label"] == ref["label"]
            ),
            default=0.0,
        )
        reasons.append(
            reason("loose_box" if closest >= LOOSE_IOU else "unseen_label", ref)
        )
    if feet:
        reasons.extend(foot_reasons(pairs))
    reasons.sort(key=lambda r: -WEIGHTS[r["kind"]] * r.get("confidence", 1.0))
    return reasons


def match_predictions(
    predicted: list[dict[str, Any]], approved: list[dict[str, Any]]
) -> tuple[list[tuple[dict, dict]], set[int], list[dict[str, Any]]]:
    """Pair confident-first predictions with labels; name confident leftovers."""
    used: set[int] = set()
    pairs = []
    reasons = []
    for obj in predicted:
        confidence = obj.get("confidence", 0)
        overlaps = [
            (iou(obj["bbox"], ref["bbox"]), j)
            for j, ref in enumerate(approved)
            if j not in used
        ]
        same = max(
            ((o, j) for o, j in overlaps if approved[j]["label"] == obj["label"]),
            default=(0.0, -1),
        )
        if same[0] >= MATCH_IOU:
            used.add(same[1])
            pairs.append((obj, approved[same[1]]))
            continue
        other = max(
            ((o, j) for o, j in overlaps if approved[j]["label"] != obj["label"]),
            default=(0.0, -1),
        )
        if (
            other[0] >= MATCH_IOU
            and confidence >= ROLE_CONFIDENCE
            and {obj["label"], approved[other[1]]["label"]} <= ROLES
        ):
            used.add(other[1])
            reasons.append(reason("wrong_role", obj, confidence))
        elif confidence >= MISSING_CONFIDENCE and all(
            iou(obj["bbox"], ref["bbox"]) < LOOSE_IOU for ref in approved
        ):
            reasons.append(reason("unlabelled", obj, confidence))
    return pairs, used, reasons


def foot_reasons(pairs: list[tuple[dict, dict]]) -> list[dict[str, Any]]:
    """Compare pole feet on matched baskets where the model placed one."""
    reasons = []
    for obj, ref in pairs:
        if obj["label"] != "basket" or not isinstance(obj.get("post_foot"), list):
            continue
        if not isinstance(ref.get("post_foot"), list):
            reasons.append(reason("missing_post_foot", ref))
        elif (
            math.dist(obj["post_foot"], ref["post_foot"]) > FOOT_DISTANCE
            and obj.get("confidence", 0) >= ROLE_CONFIDENCE
        ):
            reasons.append(reason("post_foot_off", ref))
    return reasons


def reason(kind: str, obj: dict[str, Any], confidence: float | None = None) -> dict:
    """Build a compact, reviewer-facing hint anchored to one box."""
    return {
        "kind": kind,
        "label": obj["label"],
        "bbox": [round(v, 4) for v in obj["bbox"]],
        **({"confidence": round(confidence, 3)} if confidence is not None else {}),
    }


def score(reasons: list[dict[str, Any]]) -> float:
    """Confidence-weighted disagreement; two loose boxes also reach FLAG_SCORE."""
    return round(sum(WEIGHTS[r["kind"]] * r.get("confidence", 1.0) for r in reasons), 3)


def check(
    dataset: Path,
    manifest: dict[str, Any],
    predict: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    """Score every frozen frame against the labels it was frozen with."""
    feet = manifest.get("task") == "pose"
    frames: list[dict[str, Any]] = []
    for record in manifest["frames"]:
        reasons = disagreements(
            predict(dataset / record["image"]),
            record["annotation"],
            manifest["classes"],
            feet=feet,
        )
        frames.append({
            "match_id": record["match_id"],
            "frame_id": record["frame_id"],
            "frame_version": record["frame_version"],
            "split": record["split"],
            "score": score(reasons),
            "reasons": reasons[:MAX_REASONS],
        })
    frames.sort(key=lambda f: -f["score"])
    return {"version": VERSION, "snapshot": manifest["id"], "frames": frames}
