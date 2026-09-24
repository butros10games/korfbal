"""Combine consistent basket observations for an automatic temporal reference."""

from __future__ import annotations

from operator import itemgetter
from typing import TYPE_CHECKING, Any

from .clip_signals import modules


if TYPE_CHECKING:
    from numpy.typing import NDArray

MIN_CONFIDENCE = 0.45
MIN_OBSERVATIONS = 3
MIN_OVERLAP = 0.35


def observations(objects: list, warp: NDArray[Any], timestamp: float) -> list:
    """Map actual detections into the composite frame without inferring hidden boxes."""
    _, np = modules()
    found = []
    for obj in objects:
        if obj["label"] != "basket" or obj.get("confidence", 0) < MIN_CONFIDENCE:
            continue
        x, y, w, h = obj["bbox"]
        points = (
            np.array([[x, y, 1], [x + w, y, 1], [x + w, y + h, 1], [x, y + h, 1]])
            @ warp.T
        )
        if not np.isfinite(points).all() or (points[:, 2] <= 0).any():
            continue
        xy = points[:, :2] / points[:, 2:]
        bounds = np.r_[xy.min(axis=0), xy.max(axis=0)]
        if (bounds < 0).any() or (bounds > 1).any():
            continue
        found.append({
            "bounds": bounds,
            "confidence": obj["confidence"],
            "time": timestamp,
        })
    return found


def overlap(a: NDArray[Any], b: NDArray[Any]) -> float:
    """Compare aligned boxes, requiring agreement in location and size."""
    _, np = modules()
    intersection = float(
        np.maximum(0, np.minimum(a[2:], b[2:]) - np.maximum(a[:2], b[:2])).prod()
    )
    union = float((a[2:] - a[:2]).prod() + (b[2:] - b[:2]).prod()) - intersection
    return intersection / max(union, 1e-9)


def combine(votes: list) -> list:
    """Require several separate frames, not several detections in the same image."""
    _, np = modules()
    groups: list[list] = []
    for vote in sorted(votes, key=itemgetter("confidence"), reverse=True):
        group = next(
            (
                g
                for g in groups
                if overlap(g[0]["bounds"], vote["bounds"]) >= MIN_OVERLAP
            ),
            None,
        )
        if group is None:
            groups.append([vote])
        elif all(v["time"] != vote["time"] for v in group):
            group.append(vote)
    result = []
    for group in groups:
        if len(group) < MIN_OBSERVATIONS:
            continue
        x1, y1, x2, y2 = np.median([v["bounds"] for v in group], axis=0)
        result.append({
            "label": "basket",
            "bbox": [float(x1), float(y1), float(x2 - x1), float(y2 - y1)],
            "confidence": float(np.median([v["confidence"] for v in group])),
            "observations": len(group),
        })
    return result
