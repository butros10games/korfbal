"""Floor positions relative to the two standard korfball posts."""

from __future__ import annotations

import math


def post_positions(court: dict) -> list[list[float]]:
    """Place each post one third into its half, on the court's width centreline."""
    return [
        [court["length"] * fraction, court["width"] / 2] for fraction in (1 / 6, 5 / 6)
    ]


def attach_post_distances(objects: list[dict], court: dict | None) -> None:
    """Measure observed player footpoints only when a valid floor projection exists."""
    for obj in objects:
        point = obj.get("court_xy_m")
        obj["post_distances_m"] = (
            [round(math.dist(point, post), 2) for post in post_positions(court)]
            if court and point is not None and obj["label"] == "player"
            else None
        )


def penalty_arcs(court: dict) -> list[list[list[float]]]:
    """Sample the rear and front semicircles of the standard 2.5 m penalty area."""
    arcs = []
    for post, direction in zip(post_positions(court), (1, -1), strict=True):
        for offset, start in ((0, math.pi / 2), (2.5, -math.pi / 2)):
            arcs.append([
                [
                    post[0]
                    + direction * (offset + 2.5 * math.cos(start + math.pi * i / 16)),
                    post[1] + 2.5 * math.sin(start + math.pi * i / 16),
                ]
                for i in range(17)
            ])
    return arcs
