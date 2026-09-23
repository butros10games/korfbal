"""Visible floor geometry; distant corners may lie behind a broadcast camera."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .clip_signals import modules


if TYPE_CHECKING:
    from numpy.typing import NDArray

EPSILON = 1e-8
MIN_AREA = 0.01
MIN_POLYGON_VERTICES = 3


def orient(floor: NDArray[Any], points: NDArray[Any]) -> NDArray[Any]:
    """Choose the visible sheet using observed points, never an offscreen corner.

    Raises:
        ValueError: The observations cross the projection horizon.

    """
    _, np = modules()
    depths = np.c_[points, np.ones(len(points))] @ floor[2]
    scale = float(np.mean(depths))
    if abs(scale) < EPSILON or (depths / scale <= EPSILON).any():
        raise ValueError("Reference landmarks cross the image horizon")
    return floor / scale


def clip_polygon(polygon: list, plane: NDArray[Any]) -> list:
    """Clip image points against one homogeneous world-space inequality."""
    _, np = modules()
    result = []
    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        va, vb = float(plane @ [*a, 1]), float(plane @ [*b, 1])
        if va >= 0:
            result.append(a)
        if (va >= 0) != (vb >= 0):
            result.append((np.array(a) + (np.array(b) - a) * va / (va - vb)).tolist())
    return result


def footprint(floor: NDArray[Any], court: dict) -> list:
    """Return only the court that is in front of the camera and inside the image.

    Raises:
        ValueError: Too little stable floor is visible.

    """
    cv, np = modules()
    if not np.isfinite(floor).all() or abs(np.linalg.det(floor)) < EPSILON:
        raise ValueError("Unstable court projection")
    polygon = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    for plane in (
        floor[2] - [0, 0, EPSILON],
        floor[0],
        court["length"] * floor[2] - floor[0],
        floor[1],
        court["width"] * floor[2] - floor[1],
    ):
        polygon = clip_polygon(polygon, plane)
    if (
        len(polygon) < MIN_POLYGON_VERTICES
        or abs(cv.contourArea(np.float32(polygon))) < MIN_AREA
    ):
        raise ValueError("Too little visible court for a stable projection")
    return polygon


def segment(a: list | tuple, b: list | tuple, inverse: NDArray[Any]) -> tuple | None:
    """Clip a world line before division, so a horizon cannot create a false line."""
    _, np = modules()
    start, end = inverse @ [*a, 1], inverse @ [*b, 1]
    lo, hi = 0.0, 1.0
    for plane in np.array([[0, 0, 1], [1, 0, 0], [-1, 0, 1], [0, 1, 0], [0, -1, 1]]):
        va, vb = float(plane @ start) - EPSILON, float(plane @ end) - EPSILON
        if va < 0 and vb < 0:
            return None
        if (va < 0) != (vb < 0):
            crossing = va / (va - vb)
            if va < 0:
                lo = max(lo, crossing)
            else:
                hi = min(hi, crossing)
    if lo >= hi:
        return None
    points = [start + t * (end - start) for t in (lo, hi)]
    return tuple((p[:2] / p[2]).clip(0, 1).tolist() for p in points)


def exclude_overlays(mask: NDArray[Any]) -> None:
    """Reserve common broadcast-graphic bands; fixed captions are not floor texture."""
    height = mask.shape[0]
    mask[: round(height * 0.12)] = 0
    mask[round(height * 0.84) :] = 0
