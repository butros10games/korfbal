"""Shared normalized bounding-box geometry."""


def iou(a: list[float], b: list[float]) -> float:
    """Intersection over union for normalized xywh boxes."""
    overlap = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(
        0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    )
    return overlap / (a[2] * a[3] + b[2] * b[3] - overlap)
