"""Reacquire automatic court references with independent floor-edge validation."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any

from . import clip_geometry
from .clip_court import CourtMap


if TYPE_CHECKING:
    from numpy.typing import NDArray

MAX_REFERENCES = 24
MIN_EDGE_POINTS = 60
MIN_EDGE_SUPPORT = 0.7
MIN_SPREAD_X = 80
MIN_SPREAD_Y = 12
EDGE_DISTANCE = 2.5


class References:
    """Match stationary scenery, then test the floor separately for parallax."""

    def __init__(self, matcher: CourtMap) -> None:
        """Keep a bounded bank of original observations, never propagated images."""
        self.matcher = matcher
        self.items: deque = deque(maxlen=MAX_REFERENCES)

    def add(
        self,
        image: NDArray[Any],
        timestamp: float,
        objects: list,
        found: tuple,
        *,
        observation: tuple | None = None,
    ) -> bool:
        """Retain a usable reference; abstain on a tiny or unstable floor."""
        floor, evidence = found
        boxes = [o["bbox"] for o in objects]
        scene = self.matcher.features(image, boxes)
        try:
            ground, patch = self.ground_reference(image, boxes, found, observation)
        except (ValueError, self.matcher.np.linalg.LinAlgError):
            return False
        self.items.append({
            "time": timestamp,
            "floor": floor.copy(),
            "evidence": evidence.copy(),
            "scene": scene,
            "ground": ground,
            "patch": patch,
        })

        return True

    def ground_reference(
        self, image: NDArray[Any], boxes: list, found: tuple, observation: tuple | None
    ) -> tuple:
        """Bound independently verified pixels to the locally measured area."""
        floor, evidence = found
        ground = self.matcher.features(
            image if observation is None else observation[0],
            boxes if observation is None else [],
            floor,
        )
        spot = self.matcher.np.r_[evidence["observed_spot"], 1.0] @ floor.T
        spot = spot[:2] / spot[2]
        post = self.matcher.court["length"] * (
            1 / 6 if spot[0] < self.matcher.court["length"] / 2 else 5 / 6
        )
        patch = (min(spot[0], post) - 3.5, spot[1] - 3.5, abs(spot[0] - post) + 7, 7.0)
        self.limit_ground(ground, floor, patch)
        if observation is not None:
            cv = self.matcher.cv
            ground[1][
                cv.resize(observation[1], (640, 360), interpolation=cv.INTER_NEAREST)
                == 0
            ] = 0
        return ground, patch

    def limit_ground(self, features: tuple, floor: NDArray[Any], patch: tuple) -> None:
        """Restrict validation to the observed penalty area."""
        cv, np = self.matcher.cv, self.matcher.np
        x, y, width, height = patch
        local = np.array([[1, 0, -x], [0, 1, -y], [0, 0, 1]]) @ floor
        polygon = clip_geometry.footprint(local, {"length": width, "width": height})
        mask = np.zeros_like(features[1])
        cv.fillConvexPoly(mask, np.int32(np.array(polygon) * [640, 360]), 255)
        features[1][:] &= mask

    def floor_agreement(
        self, reference: tuple, current: tuple, warp: NDArray[Any]
    ) -> float:
        """Reject background matches that move the court differently from the wall."""
        cv, np = self.matcher.cv, self.matcher.np
        scale = np.diag([640.0, 360.0, 1.0])
        # The registration maps current pixels to the original reference.
        pixels = scale @ np.linalg.inv(warp) @ np.linalg.inv(scale)
        mask = cv.warpPerspective(
            reference[1], pixels, (640, 360), flags=cv.INTER_NEAREST
        )
        mask &= current[1]
        mask = cv.erode(mask, np.ones((5, 5), np.uint8))
        source = cv.warpPerspective(reference[0], pixels, (640, 360))
        edges = [
            cv.Canny(cv.GaussianBlur(gray, (3, 3), 0.7), 15, 45)
            for gray in (source, current[0])
        ]
        scores = []
        for a, b in (edges, edges[::-1]):
            ys, xs = np.nonzero((a > 0) & (mask > 0))
            if (
                len(xs) < MIN_EDGE_POINTS
                or np.ptp(xs) < MIN_SPREAD_X
                or np.ptp(ys) < MIN_SPREAD_Y
            ):
                return 0.0
            distance = cv.distanceTransform(255 - b, cv.DIST_L2, 3)
            scores.append(float((distance[ys, xs] <= EDGE_DISTANCE).mean()))
        return min(scores)

    def find(
        self, image: NDArray[Any], timestamp: float, objects: list
    ) -> tuple | None:
        """Independently register a saved view; never carry a cut's old transform."""
        current = self.matcher.features(image, [o["bbox"] for o in objects])
        for reference in sorted(self.items, key=lambda r: abs(timestamp - r["time"])):
            match = self.matcher.register(current, reference["scene"])
            if match is None:
                continue
            candidate = reference["floor"] @ match[0]
            try:
                ground = self.matcher.features(
                    image, [o["bbox"] for o in objects], candidate
                )
                self.limit_ground(ground, candidate, reference["patch"])
                agreement = self.floor_agreement(reference["ground"], ground, match[0])
            except (ValueError, self.matcher.np.linalg.LinAlgError):
                continue
            if agreement < MIN_EDGE_SUPPORT:
                continue
            return candidate, {
                **reference["evidence"],
                "status": "automatic_reference",
                "reference_time": reference["time"],
                "inliers": match[1],
                "error_pixels": round(match[2], 3),
                "floor_support": round(agreement, 3),
            }
        return None
