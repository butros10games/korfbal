"""Shirt-number patch geometry: upper-body crop, patch-detector I/O, reader patch.

Pure NumPy/OpenCV so the offline training tools and the clip worker share one
implementation. Boxes inside a crop are relative ``[x1, y1, x2, y2]`` in 0..1.
"""

from __future__ import annotations

from typing import Any

from .clip_signals import modules


PATCH = (128, 32)  # reader input width, height
MARGIN = (0.18, 0.12)  # horizontal, vertical expansion of a detected number box
DETECTOR_SIZE = 160
PAD = 114
MIN_HEIGHT_720 = 40  # person boxes shorter than this (at 720p scale) are unreadable
UPPER = (0.12, 0.02, 0.68)  # side margin, top margin, bottom (fractions of the box)
MIN_CROP = (8, 12)  # smallest usable upper-body crop width, height in pixels


def upper_body(image: Any, box: list[float]) -> Any | None:  # noqa: ANN401
    """Cut shoulders-to-hips plus margin from a normalized ``[x, y, w, h]`` box."""
    h, w = image.shape[:2]
    x, y, bw, bh = box
    if bh * 720 < MIN_HEIGHT_720:
        return None
    x1, y1, x2, y2 = x * w, y * h, (x + bw) * w, (y + bh) * h
    pw, ph = x2 - x1, y2 - y1
    left, right = max(0, round(x1 - UPPER[0] * pw)), min(w, round(x2 + UPPER[0] * pw))
    top, bottom = max(0, round(y1 - UPPER[1] * ph)), min(h, round(y1 + UPPER[2] * ph))
    if right - left < MIN_CROP[0] or bottom - top < MIN_CROP[1]:
        return None
    return image[top:bottom, left:right].copy()


def detector_input(crops: list) -> tuple[Any, list[tuple[float, float, float]]]:
    """Letterbox crops to the square detector size; return batch and undo params."""
    cv, np = modules()
    batch, undo = [], []
    for crop in crops:
        h, w = crop.shape[:2]
        s = DETECTOR_SIZE / max(h, w)
        rw, rh = max(1, round(w * s)), max(1, round(h * s))
        canvas = np.full((DETECTOR_SIZE, DETECTOR_SIZE, 3), PAD, np.uint8)
        ox, oy = (DETECTOR_SIZE - rw) // 2, (DETECTOR_SIZE - rh) // 2
        canvas[oy : oy + rh, ox : ox + rw] = cv.resize(crop, (rw, rh))
        batch.append(canvas[..., ::-1].transpose(2, 0, 1))
        undo.append((s, ox, oy))
    return np.stack(batch).astype(np.float32) / 255, undo


def best_boxes(
    output: Any,  # noqa: ANN401
    crops: list,
    undo: list[tuple[float, float, float]],
) -> list[tuple[list[float], float]]:
    """Strongest single-class YOLO box per crop, as a relative box and confidence."""
    _, np = modules()
    result = []
    for prediction, crop, (s, ox, oy) in zip(output, crops, undo, strict=True):
        k = int(np.argmax(prediction[4]))
        cx, cy, bw, bh, conf = (float(v) for v in prediction[:5, k])
        h, w = crop.shape[:2]
        x1, x2 = ((cx - bw / 2 - ox) / s) / w, ((cx + bw / 2 - ox) / s) / w
        y1, y2 = ((cy - bh / 2 - oy) / s) / h, ((cy + bh / 2 - oy) / s) / h
        box = [min(max(v, 0.0), 1.0) for v in (x1, y1, x2, y2)]
        result.append((box, conf))
    return result


def reader_patch(crop: Any, box: list[float]) -> Any:  # noqa: ANN401
    """Cut the number box (plus margin) and letterbox it, aspect kept, to 128x32."""
    cv, np = modules()
    h, w = crop.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    left = int(max(0, np.floor((x1 - MARGIN[0] * bw) * w)))
    right = int(min(w, np.ceil((x2 + MARGIN[0] * bw) * w)))
    top = int(max(0, np.floor((y1 - MARGIN[1] * bh) * h)))
    bottom = int(min(h, np.ceil((y2 + MARGIN[1] * bh) * h)))
    if right - left < 2 or bottom - top < 2:  # noqa: PLR2004 - degenerate box
        return np.full((PATCH[1], PATCH[0], 3), 127, np.uint8)
    region = crop[top:bottom, left:right]
    s = min(PATCH[0] / region.shape[1], PATCH[1] / region.shape[0])
    rw, rh = max(1, round(region.shape[1] * s)), max(1, round(region.shape[0] * s))
    region = cv.resize(
        region, (rw, rh), interpolation=cv.INTER_AREA if s < 1 else cv.INTER_CUBIC
    )
    canvas = np.empty((PATCH[1], PATCH[0], 3), np.uint8)
    canvas[:] = np.median(region.reshape(-1, 3), axis=0).astype(np.uint8)
    ox, oy = (PATCH[0] - rw) // 2, (PATCH[1] - rh) // 2
    canvas[oy : oy + rh, ox : ox + rw] = region
    return canvas
