"""Editable landmark proposals from an already registered human court reference."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .clip_contract import landmark_points
from .clip_positions import post_positions
from .clip_signals import modules, transform


if TYPE_CHECKING:
    from numpy.typing import NDArray

MAX_SUGGESTED_POINTS = 8
MIN_SUGGESTED_POINTS = 4
MIN_ARC_SUPPORT = 2
EDGE_MARGIN = 0.01
OBJECT_MARGIN = 0.008


def floor_landmarks(court: dict) -> list[list[float]]:
    """Return boundaries, post bases and metric penalty-area reference points."""
    length, width = court["length"], court["width"]
    points = [[x, y] for x in (0, length / 2, length) for y in (0, width)]
    for (x, y), direction in zip(post_positions(court), (1, -1), strict=True):
        points.extend([
            [x, y],
            [x + direction * 2.5, y],
            [x - direction * 2.5, y],
            [x + direction * 5, y],
            [x, y - 2.5],
            [x, y + 2.5],
            [x + direction * 2.5, y - 2.5],
            [x + direction * 2.5, y + 2.5],
        ])
    return points


def suggestion(
    floor: NDArray[Any] | None, court: dict | None, evidence: dict, boxes: list
) -> list[dict]:
    """Suggest visible points only after fresh reference and line/arc support.

    These proposals are never calibration inputs until someone confirms them.
    Do not bootstrap a mapping from a basket rim, unsupported oval or old warp.
    """
    if (
        floor is None
        or not court
        or evidence.get("status") != "reference"
        or not (
            evidence.get("supporting_lines")
            or evidence.get("supporting_arcs", 0) >= MIN_ARC_SUPPORT
        )
    ):
        return []
    _, np = modules()
    try:
        inverse = np.linalg.inv(floor)
    except np.linalg.LinAlgError:
        return []
    available = []
    for point in floor_landmarks(court):
        image = transform(point, inverse)
        normalized = [point[0] / court["length"], point[1] / court["width"]]
        if image is None or any(not 0 <= v <= 1 for v in normalized):
            continue
        visible = all(EDGE_MARGIN < v < 1 - EDGE_MARGIN for v in image)
        blocked = any(
            x - OBJECT_MARGIN <= image[0] <= x + w + OBJECT_MARGIN
            and y - OBJECT_MARGIN <= image[1] <= y + h + OBJECT_MARGIN
            for x, y, w, h in boxes
        )
        if visible and not blocked:
            available.append({"image": image, "court": normalized})
    if len(available) < MIN_SUGGESTED_POINTS:
        return []
    # Prefer points that expand the footprint, not eight neighbouring arc samples.
    selected = [available.pop(0)]
    while available and len(selected) < MAX_SUGGESTED_POINTS:
        index = max(
            range(len(available)),
            key=lambda i: min(
                sum(
                    (a - b) ** 2
                    for a, b in zip(available[i]["image"], p["image"], strict=True)
                )
                for p in selected
            ),
        )
        selected.append(available.pop(index))
    try:
        return landmark_points(selected)
    except ValueError:
        return []
