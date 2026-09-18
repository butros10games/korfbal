"""Optional, bounded small-ball inference; preserve all non-ball detections."""

from __future__ import annotations

from copy import deepcopy
import importlib
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING

from .store import MAX_OBJECTS, validate_annotation
from .vision import iou


if TYPE_CHECKING:
    from .training import Detector, RunOptions

MERGE_IOU = 0.4
EDGE_MARGIN = 2


def project_ball(
    box: list[float],
    confidence: float,
    tile: tuple[int, int, int, int],
    size: tuple[int, int],
) -> dict | None:
    """Map one crop detection to source pixels, rejecting cut fragments."""
    a, b, c, d = box
    if not 0 <= a < c <= 1 or not 0 <= b < d <= 1:
        return None
    x0, y0, x1, y1 = tile
    width, height = size
    inner_edges = (x0 > 0, y0 > 0, x1 < width, y1 < height)
    distances = (a * (x1 - x0), b * (y1 - y0), (1 - c) * (x1 - x0), (1 - d) * (y1 - y0))
    if any(
        inner and distance < EDGE_MARGIN
        for inner, distance in zip(inner_edges, distances, strict=True)
    ):
        return None
    return {
        "label": "ball",
        "confidence": float(confidence),
        "bbox": [
            (x0 + a * (x1 - x0)) / width,
            (y0 + b * (y1 - y0)) / height,
            (c - a) * (x1 - x0) / width,
            (d - b) * (y1 - y0) / height,
        ],
    }


def augment(model: Detector, image: Path, options: RunOptions, baseline: dict) -> dict:
    """Inspect four overlapping crops without creating training annotations.

    Raises:
        ValueError: If the source image cannot be decoded.

    """
    classes = [
        int(k) for k, name in model.names.items() if name in {"ball", "sports ball"}
    ]
    if not classes:
        return baseline
    cv2 = importlib.import_module("cv2")
    pixels = cv2.imread(str(image))
    if pixels is None:
        raise ValueError("Cannot decode ball-crop source image")
    height, width = pixels.shape[:2]
    balls = [deepcopy(o) for o in baseline["objects"] if o["label"] == "ball"]
    for top in (0.0, 0.4):
        for left in (0.0, 0.4):
            x0, y0 = round(left * width), round(top * height)
            x1, y1 = (
                min(width, round((left + 0.6) * width)),
                min(height, round((top + 0.6) * height)),
            )
            result = model.predict(
                pixels[y0:y1, x0:x1],
                device=options.device,
                imgsz=options.imgsz,
                conf=options.confidence,
                classes=classes,
                max_det=20,
                verbose=False,
            )[0]
            if result.boxes is None:
                continue
            for box, confidence in zip(
                result.boxes.xyxyn.cpu().tolist(),
                result.boxes.conf.cpu().tolist(),
                strict=True,
            ):
                ball = project_ball(box, confidence, (x0, y0, x1, y1), (width, height))
                if ball is not None:
                    balls.append(ball)
    merged = []
    for ball in sorted(balls, key=itemgetter("confidence"), reverse=True):
        if not any(iou(ball["bbox"], other["bbox"]) >= MERGE_IOU for other in merged):
            merged.append(ball)
    result = deepcopy(baseline)
    result["objects"] = [o for o in result["objects"] if o["label"] != "ball"]
    result["objects"].extend(merged[: MAX_OBJECTS - len(result["objects"])])
    return validate_annotation(result)
