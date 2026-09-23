"""Conservative camera, jersey and floor signals in the optional vision runtime."""

from __future__ import annotations

from collections import deque
import importlib
import math
from types import ModuleType
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from numpy.typing import NDArray

EPSILON = 1e-6
NEIGHBOURS = 2
MIN_FEATURES = 16
MIN_INLIERS = 0.55
MIN_SPREAD_X = 220
MIN_SPREAD_Y = 90
MAX_WARP = 5
CUT_ERROR = 0.12
MIN_SHIRT_VALUES = 90
MAX_SHIRT_SPREAD = 40
MIN_TEAM_SAMPLES = 40
MIN_CLUSTER_SAMPLES = 8
MIN_COLOR_MARGIN = 0.3
MAX_COLOR_DISTANCE = 55
MIN_TEAM_VOTES = 3
MIN_VOTE_SHARE = 0.8
COURT_MARGIN = 5


def modules() -> tuple[ModuleType, ModuleType]:
    """Keep OpenCV/NumPy out of Django web startup."""
    return importlib.import_module("cv2"), importlib.import_module("numpy")


def transform(
    point: list[float] | tuple[float, float], matrix: NDArray[Any]
) -> list[float] | None:
    """Project a point, rejecting unstable or infinite homogeneous coordinates."""
    _, np = modules()
    result = matrix @ np.array([*point, 1.0])
    if abs(result[2]) < EPSILON:
        return None
    result = result[:2] / result[2]
    return result.tolist() if np.isfinite(result).all() else None


class Camera:
    """Register the calibration frame and detect visual discontinuities."""

    def __init__(self, court: dict | None = None) -> None:
        """Initialize optional floor calibration and camera feature state."""
        self.cv, self.np = modules()
        self.orb = self.cv.ORB_create(nfeatures=1000)
        self.matcher = self.cv.BFMatcher(self.cv.NORM_HAMMING)
        self.previous = None
        self.reference = None
        self.court = court
        self.floor = None
        self.segment = 0
        self.mapping = (
            importlib.import_module(f"{__package__}.clip_court").CourtMap(court)
            if court and court.get("anchors")
            else None
        )

    def features(self, image: NDArray[Any]) -> tuple:
        """Extract bounded-resolution camera features."""
        small = self.cv.resize(image, (640, 360))
        gray = self.cv.cvtColor(small, self.cv.COLOR_BGR2GRAY)
        keys, descriptors = self.orb.detectAndCompute(gray, None)
        return gray, keys, descriptors

    def register(self, source: tuple, target: tuple) -> NDArray[Any] | None:
        """Accept only spatially distributed RANSAC support, not one moving player."""
        _, keys_a, descriptors_a = source
        _, keys_b, descriptors_b = target
        if descriptors_a is None or descriptors_b is None:
            return None
        matches = self.matcher.knnMatch(descriptors_a, descriptors_b, k=2)
        good = [
            a
            for pair in matches
            if len(pair) == NEIGHBOURS
            for a, b in [pair]
            if a.distance < 0.7 * b.distance
        ]
        if len(good) < MIN_FEATURES:
            return None
        points_a = self.np.float32([keys_a[m.queryIdx].pt for m in good])
        points_b = self.np.float32([keys_b[m.trainIdx].pt for m in good])
        matrix, mask = self.cv.findHomography(points_a, points_b, self.cv.RANSAC, 2.5)
        if (
            matrix is None
            or mask is None
            or mask.mean() < MIN_INLIERS
            or mask.sum() < MIN_FEATURES
        ):
            return None
        for points in (points_a, points_b):
            supported = points[mask.ravel().astype(bool)]
            if (
                self.np.ptp(supported[:, 0]) < MIN_SPREAD_X
                or self.np.ptp(supported[:, 1]) < MIN_SPREAD_Y
            ):
                return None
        # Express the warp in normalized image coordinates, independent of resolution.
        scale = self.np.diag([640.0, 360.0, 1.0])
        matrix = self.np.linalg.inv(scale) @ matrix @ scale
        corners = [transform(p, matrix) for p in ((0, 0), (1, 0), (1, 1), (0, 1))]
        if any(p is None or max(abs(v) for v in p) > MAX_WARP for p in corners):
            return None
        return matrix

    def update(
        self, image: NDArray[Any], timestamp: float = 0, boxes: list | None = None
    ) -> dict:
        """Return camera motion, scene boundary and an optional current floor map."""
        current = self.features(image)
        first = self.previous is None
        motion = self.np.eye(3) if first else self.register(self.previous, current)
        error = (
            0
            if first
            else self.np.mean(self.np.abs(current[0].astype(float) - self.previous[0]))
            / 255
        )
        cut = bool(not first and motion is None and error > CUT_ERROR)
        if cut:
            self.segment += 1
        if first:
            self.reference = current
            if self.court and not self.mapping:
                c = self.court
                destination = self.np.float32([
                    [0, 0],
                    [c["length"], 0],
                    [c["length"], c["width"]],
                    [0, c["width"]],
                ])
                self.floor = self.cv.getPerspectiveTransform(
                    self.np.float32(c["corners"]), destination
                )
        # Legacy single-frame corners cannot recover after a cut. Timestamped
        # references below reacquire independently in the new view.
        if cut:
            self.floor = None
        floor = None
        if self.floor is not None and self.reference is not None:
            to_reference = (
                self.np.eye(3) if first else self.register(current, self.reference)
            )
            if to_reference is not None:
                floor = self.floor @ to_reference
        self.previous = current
        calibration = {
            "status": "legacy" if floor is not None else "unknown",
            "segments": [],
        }
        if self.mapping:
            floor, calibration = self.mapping.update(image, timestamp, boxes or [], cut)
        return {
            "cut": cut,
            "segment": self.segment,
            "motion": motion,
            "floor": floor,
            "calibration": calibration,
        }


class Teams:
    """Track-level shirt-colour votes; A/B are visual groups, not player identities."""

    def __init__(self, colors: list[list[float]] | None = None) -> None:
        """Use supplied shirt colours or learn a separated palette from observations."""
        self.cv, self.np = modules()
        self.samples = deque(maxlen=160)
        self.votes = {}
        self.centers = None
        if colors is not None:
            rgb = self.np.uint8([colors])
            self.centers = self.cv.cvtColor(rgb, self.cv.COLOR_RGB2LAB)[0].astype(float)

    def observe(self, image: NDArray[Any], box: list[float]) -> NDArray[Any] | None:
        """Take a robust colour sample from the central upper torso."""
        h, w = image.shape[:2]
        x, y, bw, bh = box
        crop = image[
            max(0, int((y + bh * 0.18) * h)) : min(h, int((y + bh * 0.48) * h)),
            max(0, int((x + bw * 0.25) * w)) : min(w, int((x + bw * 0.75) * w)),
        ]
        if crop.size < MIN_SHIRT_VALUES:
            return None
        lab = self.cv.cvtColor(crop, self.cv.COLOR_BGR2LAB).reshape(-1, 3).astype(float)
        color = self.np.median(lab, axis=0)
        # Background/overlap-dominated crops should not vote for a shirt colour.
        if self.np.median(self.np.linalg.norm(lab - color, axis=1)) > MAX_SHIRT_SPREAD:
            return None
        return color

    def update(self, image: NDArray[Any], objects: list[dict]) -> None:
        """Accumulate consistent evidence before assigning a visual team group."""
        samples = [
            (o, self.observe(image, o["observed_bbox"]))
            for o in objects
            if o["label"] == "player" and not o.get("estimated")
        ]
        if self.centers is None:
            self.samples.extend(c for _, c in samples if c is not None)
            if len(self.samples) >= MIN_TEAM_SAMPLES:
                points = self.np.array(self.samples)
                # Deterministic two-means with well-separated seeds.
                first = points[0]
                centers = self.np.array([
                    first,
                    points[self.np.argmax(self.np.linalg.norm(points - first, axis=1))],
                ])
                for _ in range(12):
                    assignment = self.np.argmin(
                        self.np.linalg.norm(points[:, None] - centers, axis=2), axis=1
                    )
                    if (
                        min((assignment == n).sum() for n in (0, 1))
                        < MIN_CLUSTER_SAMPLES
                    ):
                        break
                    centers = self.np.array([
                        self.np.median(points[assignment == n], axis=0) for n in (0, 1)
                    ])
                else:
                    spreads = [
                        self.np.median(
                            self.np.linalg.norm(
                                points[assignment == n] - centers[n], axis=1
                            )
                        )
                        for n in (0, 1)
                    ]
                    if self.np.linalg.norm(centers[0] - centers[1]) > max(
                        35, 3 * max(spreads)
                    ):
                        self.centers = centers
        for obj, color in samples:
            obj.update(team="unknown", team_score=0.0)
            if self.centers is None or color is None:
                continue
            distance = self.np.linalg.norm(self.centers - color, axis=1)
            winner = int(self.np.argmin(distance))
            margin = float(abs(distance[0] - distance[1]) / max(1, distance.sum()))
            if margin < MIN_COLOR_MARGIN or distance[winner] > MAX_COLOR_DISTANCE:
                continue
            votes = self.votes.setdefault(obj["track_id"], deque(maxlen=20))
            votes.append(winner)
            score = sum(v == winner for v in votes) / len(votes)
            if len(votes) >= MIN_TEAM_VOTES and score >= MIN_VOTE_SHARE:
                obj.update(
                    team=f"team_{'ab'[winner]}", team_score=round(score * margin, 3)
                )
        current_ids = {o.get("track_id") for o in objects}
        self.votes = {k: v for k, v in self.votes.items() if k in current_ids}

    def colors(self) -> list[list[int]] | None:
        """Expose the learned palette for the legend without naming actual clubs."""
        if self.centers is None:
            return None
        return self.cv.cvtColor(self.np.uint8([self.centers]), self.cv.COLOR_LAB2RGB)[
            0
        ].tolist()


def floor_position(
    box: list[float], matrix: NDArray[Any] | None, court: dict | None
) -> list[float] | None:
    """Project an observed person's footpoint only; airborne balls have no floor XY."""
    if matrix is None or court is None:
        return None
    x, y, w, h = box
    point = transform((x + w / 2, y + h), matrix)
    if point is None or not (
        -COURT_MARGIN <= point[0] <= court["length"] + COURT_MARGIN
        and -COURT_MARGIN <= point[1] <= court["width"] + COURT_MARGIN
    ):
        return None
    return [round(v, 3) for v in point]


def center(box: list[float]) -> list[float]:
    """Return a normalized box centre."""
    return [box[0] + box[2] / 2, box[1] + box[3] / 2]


def distance(a: list[float], b: list[float]) -> float:
    """Measure Euclidean distance between two points."""
    return math.dist(a, b)
