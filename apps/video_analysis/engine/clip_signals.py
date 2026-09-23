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
MIN_SHIRT_SATURATION = 60
MIN_SHIRT_VALUE = 30
MIN_COLORED_SHARE = 0.3
MIN_HUE_SHARE = 0.25
HUE_RADIUS = 15
MIN_BACKGROUND_VALUES = 90
BACKGROUND_DISTANCE = 22
MIN_NEUTRAL_LIGHTNESS = 95
MAX_NEUTRAL_CHROMA = 18
MIN_TEAM_SAMPLES = 40
MIN_CLUSTER_SAMPLES = 8
MIN_COLOR_MARGIN = 0.55
MAX_COLOR_DISTANCE = 55
MIN_TEAM_VOTES = 3
MIN_VOTE_SHARE = 0.8
TEAM_MEMORY_SECONDS = 3.0
TEAM_HOLD_SECONDS = 2.0
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
        self.seen = {}
        self.confirmed = {}
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
        # The box includes arms and empty floor. Sample narrow outside strips at
        # torso height, and remove only a consistent neighbouring background.
        strips = [
            image[
                max(0, int((y + bh * 0.18) * h)) : min(h, int((y + bh * 0.48) * h)),
                max(0, int(a * w)) : min(w, int(b * w)),
            ].reshape(-1, 3)
            for a, b in ((x - bw * 0.2, x), (x + bw, x + bw * 1.2))
        ]
        background = self.np.concatenate(strips)
        return self.sample(crop, background)

    def sample(
        self, crop: NDArray[Any], background: NDArray[Any] | None = None
    ) -> NDArray[Any] | None:
        """Separate shirt hues from white numbers and mixed torso pixels."""
        if crop.size < MIN_SHIRT_VALUES:
            return None
        pixels = self.cv.cvtColor(crop, self.cv.COLOR_BGR2HSV).reshape(-1, 3)
        lab = self.cv.cvtColor(crop, self.cv.COLOR_BGR2LAB).reshape(-1, 3).astype(float)
        neutral = self.np.median(lab, axis=0)
        if (
            neutral[0] >= MIN_NEUTRAL_LIGHTNESS
            and self.np.linalg.norm(neutral[1:] - 128) < MAX_NEUTRAL_CHROMA
        ):
            # Preserve a white shirt's body instead of selecting a small colourful
            # number, sleeve or patch of floor from the same torso crop.
            spread = self.np.median(self.np.linalg.norm(lab - neutral, axis=1))
            return neutral if spread <= MAX_SHIRT_SPREAD else None
        if background is not None and background.size >= MIN_BACKGROUND_VALUES:
            surroundings = (
                self.cv
                .cvtColor(background.reshape(-1, 1, 3), self.cv.COLOR_BGR2LAB)
                .reshape(-1, 3)
                .astype(float)
            )
            median = self.np.median(surroundings, axis=0)
            spread = self.np.median(self.np.linalg.norm(surroundings - median, axis=1))
            foreground = self.np.linalg.norm(lab - median, axis=1) > BACKGROUND_DISTANCE
            if spread < BACKGROUND_DISTANCE and foreground.mean() >= MIN_HUE_SHARE:
                lab, pixels = lab[foreground], pixels[foreground]
        original_size = crop.shape[0] * crop.shape[1]
        saturated = (pixels[:, 1] >= MIN_SHIRT_SATURATION) & (
            pixels[:, 2] >= MIN_SHIRT_VALUE
        )
        if saturated.mean() >= MIN_COLORED_SHARE:
            hues = pixels[:, 0].astype(float)
            bins = self.np.bincount((hues[saturated] // 10).astype(int), minlength=18)
            smoothed = bins + self.np.roll(bins, 1) + self.np.roll(bins, -1)
            peak = int(self.np.argmax(smoothed)) * 10 + 5
            delta = self.np.abs(hues - peak)
            keep = saturated & (self.np.minimum(delta, 180 - delta) <= HUE_RADIUS)
            if keep.sum() >= original_size * MIN_COLORED_SHARE:
                lab = lab[keep]
            else:
                return None
        color = self.np.median(lab, axis=0)
        if self.np.median(self.np.linalg.norm(lab - color, axis=1)) > MAX_SHIRT_SPREAD:
            return None
        return color

    def reset(self) -> None:
        """Forget people at a camera cut while preserving the match's A/B palette."""
        self.votes.clear()
        self.seen.clear()
        self.confirmed.clear()

    def features(self, colors: NDArray[Any]) -> NDArray[Any]:
        """Separate dark shirt hues without treating exposure as another team.

        Circular hue coordinates also keep reds on either side of the HSV seam
        together. Saturation reduces the influence of unstable near-grey hues.
        Keep the original LAB samples for the displayed palette.
        """
        hsv = self.cv.cvtColor(
            self.cv.cvtColor(self.np.uint8([colors]), self.cv.COLOR_LAB2BGR),
            self.cv.COLOR_BGR2HSV,
        )[0].astype(float)
        angle = hsv[:, 0] * (2 * self.np.pi / 180)
        radius = hsv[:, 1] * (80 / 255)
        return self.np.column_stack((
            self.np.cos(angle) * radius,
            self.np.sin(angle) * radius,
            colors[:, 0] * 0.15,
        ))

    def learn(self, samples: list) -> None:
        """Learn two separated shirt colours without renaming established groups."""
        if self.centers is not None:
            return
        self.samples.extend(c for _, c in samples if c is not None)
        if len(self.samples) < MIN_TEAM_SAMPLES:
            return
        points = self.np.array(self.samples)
        features = self.features(points)
        first = features[0]
        centers = self.np.array([
            first,
            features[self.np.argmax(self.np.linalg.norm(features - first, axis=1))],
        ])
        for _ in range(12):
            assignment = self.np.argmin(
                self.np.linalg.norm(features[:, None] - centers, axis=2),
                axis=1,
            )
            if min((assignment == n).sum() for n in (0, 1)) < MIN_CLUSTER_SAMPLES:
                return
            centers = self.np.array([
                self.np.median(features[assignment == n], axis=0) for n in (0, 1)
            ])
        spreads = [
            self.np.median(
                self.np.linalg.norm(features[assignment == n] - centers[n], axis=1)
            )
            for n in (0, 1)
        ]
        if self.np.linalg.norm(centers[0] - centers[1]) > max(35, 3 * max(spreads)):
            self.centers = self.np.array([
                self.np.median(points[assignment == n], axis=0) for n in (0, 1)
            ])

    def vote(self, color: NDArray[Any] | None) -> tuple[int, float] | None:
        """Abstain on mixed crops and on colours outside the learned shirts."""
        if self.centers is None or color is None:
            return None
        distances = self.np.linalg.norm(
            self.features(self.centers) - self.features(self.np.array([color]))[0],
            axis=1,
        )
        winner = int(self.np.argmin(distances))
        margin = float(abs(distances[0] - distances[1]) / max(1, distances.sum()))
        if margin < MIN_COLOR_MARGIN or distances[winner] > MAX_COLOR_DISTANCE:
            return None
        return winner, margin

    def assign(self, obj: dict, color: NDArray[Any] | None, timestamp: float) -> None:
        """Hold recent confirmed evidence, but suppress contradictory observations."""
        identity = obj["track_id"]
        self.seen[identity] = timestamp
        obj.update(team="unknown", team_score=0.0, team_source="unknown")
        obj.pop("team_age_seconds", None)
        vote = self.vote(color)
        votes = self.votes.setdefault(identity, deque(maxlen=20))
        while votes and timestamp - votes[0][2] > TEAM_MEMORY_SECONDS:
            votes.popleft()
        prior = self.confirmed.get(identity)
        if vote is not None:
            winner, margin = vote
            votes.append((winner, margin, timestamp))
            share = sum(v[0] == winner for v in votes) / len(votes)
            if len(votes) >= MIN_TEAM_VOTES and share >= MIN_VOTE_SHARE:
                prior = (winner, share * margin, timestamp)
                self.confirmed[identity] = prior
            # A contradictory clear crop must not display the previous team.
            if prior is None or prior[0] != winner:
                self.confirmed.pop(identity, None)
                return
        if prior is None or timestamp - prior[2] > TEAM_HOLD_SECONDS:
            return
        age = max(0.0, timestamp - prior[2])
        obj.update(
            team=f"team_{'ab'[prior[0]]}",
            team_score=round(prior[1] * (1 - age / (TEAM_HOLD_SECONDS * 2)), 3),
            team_source="shirt" if age == 0 else "track_history",
            team_age_seconds=round(age, 3),
        )

    def update(
        self, image: NDArray[Any], objects: list[dict], timestamp: float = 0
    ) -> None:
        """Keep bounded shirt evidence through occlusion and brief detector gaps."""
        expired = [
            k for k, t in self.seen.items() if timestamp - t > TEAM_MEMORY_SECONDS
        ]
        for identity in expired:
            self.seen.pop(identity, None)
            self.votes.pop(identity, None)
            self.confirmed.pop(identity, None)
        samples = [
            (o, self.observe(image, o["observed_bbox"]))
            for o in objects
            if o["label"] == "player" and not o.get("estimated")
        ]
        self.learn(samples)
        for obj, color in samples:
            self.assign(obj, color, timestamp)

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
