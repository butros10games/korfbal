"""Pole-foot keypoints predicted with baskets by a pose-trained detector."""

from __future__ import annotations

from typing import Any, cast


MIN_FOOT_CONFIDENCE = 0.5


def post_feet(result: object, count: int) -> list[list[float] | None]:
    """One normalized pole foot (or None) for each of a result's `count` boxes.

    Box-only detectors, low-confidence keypoints and results whose boxes were
    rebuilt without keypoints give None; a foot is never guessed.
    """
    keypoints = cast("Any", getattr(result, "keypoints", None))
    if keypoints is None or count == 0 or len(keypoints) != count:
        return [None] * count
    points = keypoints.xyn.cpu().tolist()
    confidence = keypoints.conf.cpu().tolist() if keypoints.conf is not None else None
    feet: list[list[float] | None] = []
    for index, point in enumerate(points):
        if not point:
            feet.append(None)
            continue
        x, y = point[0]
        score = confidence[index][0] if confidence is not None else 1.0
        inside = 0 < x < 1 and 0 < y < 1
        feet.append(
            [float(x), float(y)] if inside and score >= MIN_FOOT_CONFIDENCE else None
        )
    return feet
