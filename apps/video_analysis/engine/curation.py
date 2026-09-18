"""Reproducible person-box comparisons and revision-bound training selections."""

import hashlib
import json

from .boxes import iou
from .store import frame_version


CAUSES = {
    "correct",
    "duplicate",
    "spectator",
    "bad_box",
    "wrong_role",
    "missed_player",
    "reference_missing",
    "reference_wrong",
    "uncertain",
}
TAGS = {"occlusion", "small_player", "motion_blur", "crowded", "camera", "lighting"}


def fingerprint(value: object) -> str:
    """Identify the exact annotations and model output seen by an auditor."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def ready(frame: dict) -> bool:
    """Require an audit of the current approved, complete correction."""
    audit = frame.get("curation", {})
    return bool(
        audit.get("complete")
        and audit.get("frame_version") == frame_version(frame)
        and frame.get("status") == "approved"
        and frame.get("complete")
        and frame.get("correction") is not None
    )


def compare(prediction: dict, reference: dict, threshold: float = 0.5) -> dict:
    """Return maximum-cardinality class-aware matches and stable object IDs.

    Scores only players/referees; unmatched boxes are questions for a human,
    not an automatic declaration that the approved reference is correct.
    """
    predicted = [
        (i, o)
        for i, o in enumerate(prediction["objects"])
        if o["label"] in {"player", "referee"}
    ]
    approved = [
        (i, o)
        for i, o in enumerate(reference["objects"])
        if o["label"] in {"player", "referee"}
    ]
    edges = {
        i: sorted(
            [
                (j, iou(p["bbox"], r["bbox"]))
                for j, r in approved
                if p["label"] == r["label"] and iou(p["bbox"], r["bbox"]) >= threshold
            ],
            key=lambda pair: (-pair[1], pair[0]),
        )
        for i, p in predicted
    }
    owners: dict[int, int] = {}

    def assign(i: int, seen: set[int]) -> bool:
        for j, _ in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owners or assign(owners[j], seen):
                owners[j] = i
                return True
        return False

    for i, _ in predicted:
        assign(i, set())
    paired = {i: j for j, i in owners.items()}
    rows = []
    for prefix, objects, matched in [("P", predicted, paired), ("R", approved, owners)]:
        for i, obj in objects:
            rows.append({
                "id": f"{prefix}{i + 1}",
                "side": "prediction" if prefix == "P" else "reference",
                "object": obj,
                "matched": i in matched,
                "partner": f"{'R' if prefix == 'P' else 'P'}{matched[i] + 1}"
                if i in matched
                else None,
            })
    return {
        "matched": len(owners),
        "unmatched_predictions": len(predicted) - len(owners),
        "unmatched_references": len(approved) - len(owners),
        "rows": rows,
    }
