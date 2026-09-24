"""Bounded refinement of a measured penalty ellipse against the full outline."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .clip_auto_court import circle_plane, project


if TYPE_CHECKING:
    from numpy.typing import NDArray

    from .clip_auto_court import Landmarks

MAX_EVALUATIONS = 45
MAX_RESIDUAL = 20


def refine(observed: Landmarks, court: dict, proposal: tuple) -> NDArray[Any] | None:
    """Correct small partial-arc fitting errors; never initialize an unseen shape."""
    np = observed.np
    ellipse, spot, pole, pieces = proposal
    unit = project(observed.ellipse_map(ellipse), [pole])[0]
    initial = np.array([
        *ellipse[0],
        *ellipse[1],
        ellipse[2],
        np.arctan2(unit[1], unit[0]),
    ])
    delta = np.array([15.0, 6.0, ellipse[1][0] * 0.1, ellipse[1][1] * 0.1, 2.0, 0.08])
    world = np.concatenate(pieces)

    def mapping(parameters: NDArray[Any]) -> NDArray[Any]:
        shape = (tuple(parameters[:2]), tuple(parameters[2:4]), float(parameters[4]))
        angle = parameters[5]
        end = project(
            np.linalg.inv(observed.ellipse_map(shape)), [[np.cos(angle), np.sin(angle)]]
        )[0]
        return circle_plane(shape, spot, end, court, observed.gray.shape)

    try:
        source = project(np.linalg.inv(mapping(initial)), world) * [
            observed.width,
            observed.height,
        ]
    except (ValueError, np.linalg.LinAlgError):
        return None
    pixels = np.clip(
        source.round().astype(int), [0, 0], [observed.width - 1, observed.height - 1]
    )
    keep = observed.mask[pixels[:, 1], pixels[:, 0]] > 0
    world = world[keep]

    def residual(parameters: NDArray[Any]) -> NDArray[Any]:
        try:
            points = project(np.linalg.inv(mapping(parameters)), world) * [
                observed.width,
                observed.height,
            ]
        except (ValueError, np.linalg.LinAlgError):
            return np.full(len(world), MAX_RESIDUAL, dtype=float)
        points = np.clip(points, [0, 0], [observed.width - 2, observed.height - 2])
        pixels = np.floor(points).astype(int)
        x, y = pixels.T
        dx, dy = (points - pixels).T
        distance = observed.distance
        values = (distance[y, x] * (1 - dx) + distance[y, x + 1] * dx) * (1 - dy)
        values += (distance[y + 1, x] * (1 - dx) + distance[y + 1, x + 1] * dx) * dy
        return np.minimum(values, MAX_RESIDUAL)

    result = importlib.import_module("scipy.optimize").least_squares(
        residual,
        initial,
        bounds=(initial - delta, initial + delta),
        diff_step=0.0005,
        x_scale=delta,
        max_nfev=MAX_EVALUATIONS,
        loss="soft_l1",
        f_scale=2,
    )
    try:
        return mapping(result.x)
    except (ValueError, np.linalg.LinAlgError):
        return None
