"""Short floor-only optical flow, excluding people and fixed broadcast graphics."""

from typing import Any

from . import clip_geometry as geometry
from .clip_signals import modules


MIN_POINTS = 16
MIN_SUPPORT = 0.65
MIN_SPREAD_X = 80
MIN_SPREAD_Y = 25


def correspondences(previous: tuple, current: tuple) -> tuple | None:
    """Track only visible floor features that also return to their starting point."""
    cv, np = modules()
    points = cv.goodFeaturesToTrack(previous[0], 250, 0.003, 5, mask=previous[1])
    if points is None or len(points) < MIN_POINTS:
        return None
    moved, status, _ = cv.calcOpticalFlowPyrLK(previous[0], current[0], points, None)
    if moved is None or status is None:
        return None
    a, b = points.reshape(-1, 2), moved.reshape(-1, 2)
    keep = status.ravel().astype(bool) & np.isfinite(b).all(axis=1)
    height, width = current[0].shape
    keep &= (
        (b[:, 0] >= 0) & (b[:, 0] < width - 1) & (b[:, 1] >= 0) & (b[:, 1] < height - 1)
    )
    a, b = a[keep], b[keep]
    if len(a) < MIN_POINTS:
        return None
    back, status, _ = cv.calcOpticalFlowPyrLK(current[0], previous[0], b[:, None], None)
    if back is None or status is None:
        return None
    pixels = b.round().astype(int)
    keep = status.ravel().astype(bool) & (
        np.linalg.norm(back.reshape(-1, 2) - a, axis=1) < 1
    )
    keep &= current[1][pixels[:, 1], pixels[:, 0]] > 0
    a, b = a[keep], b[keep]
    if len(a) < MIN_POINTS:
        return None
    return a, b


def register(previous: tuple, current: tuple) -> tuple | None:
    """Return current-to-previous motion with forward/backward consistency."""
    cv, np = modules()
    points = correspondences(previous, current)
    if points is None:
        return None
    a, b = points
    warp, inliers = cv.findHomography(b, a, cv.RANSAC, 1.5)
    if (
        warp is None
        or inliers is None
        or inliers.sum() < MIN_POINTS
        or inliers.mean() < MIN_SUPPORT
    ):
        return None
    keep = inliers.ravel().astype(bool)
    for spread in (a[keep], b[keep]):
        if np.ptp(spread[:, 0]) < MIN_SPREAD_X or np.ptp(spread[:, 1]) < MIN_SPREAD_Y:
            return None
    error = float(
        np.median(
            np.linalg.norm(
                cv.perspectiveTransform(b[keep][None], warp)[0] - a[keep], axis=1
            )
        )
    )
    height, width = current[0].shape
    scale: Any = np.diag([float(width), float(height), 1.0])
    normalized = np.linalg.inv(scale) @ warp @ scale
    try:
        normalized = geometry.orient(normalized, b[keep] / [width, height])
    except ValueError:
        return None
    return normalized, int(inliers.sum()), error
