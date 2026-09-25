"""Conservative camera, jersey and floor signals in the optional vision runtime."""

from __future__ import annotations

from collections import deque
import importlib
import math
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .clip_clothing import (
    MIN_VISIBLE,
    foreground,
    pixels as clothing_pixels,
    sample as clothing_sample,
)
from .clip_team_spans import TeamSpans


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
HUE_RADIUS = 15
MIN_NEUTRAL_LIGHTNESS = 95
MAX_NEUTRAL_CHROMA = 18
MIN_TEAM_SAMPLES = 40
MIN_CLUSTER_SAMPLES = 8
MIN_COLOR_MARGIN = 0.55
MAX_COLOR_DISTANCE = 55
MIN_TEAM_VOTES = 3
MIN_VOTE_SHARE = 0.8
# Players never change teams: a track keeps its whole shirt history, and a
# confirmed team outlives brief occlusion as long as the identity is certain.
TEAM_MEMORY_SECONDS = 12.0
TEAM_HALF_LIFE_SECONDS = 20.0
TEAM_VOTE_HISTORY = 64
TEAM_HOLD_SECONDS = 2.0
# A sustained run of opposite clear shirts marks a body swap, not noise.
SWITCH_VOTES = 4
SWITCH_SECONDS = 0.24
OPENING_PIXEL_SAMPLES = 256
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
        self.automatic = (
            importlib.import_module(f"{__package__}.clip_auto_court").AutoCourt(court)
            if court and court.get("mode") == "automatic"
            else None
        )

    def features(self, image: NDArray[Any]) -> tuple:
        """Extract bounded-resolution camera features."""
        small = self.cv.resize(image, (640, 360))
        gray = self.cv.cvtColor(small, self.cv.COLOR_BGR2GRAY)
        mask = self.np.full(gray.shape, 255, dtype=self.np.uint8)
        importlib.import_module(f"{__package__}.clip_geometry").exclude_overlays(mask)
        keys, descriptors = self.orb.detectAndCompute(gray, mask)
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
        self,
        image: NDArray[Any],
        timestamp: float = 0,
        boxes: list | None = None,
        objects: list | None = None,
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
        if first:
            self.reference = current
            if self.court and not self.mapping and not self.automatic:
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
        if self.automatic:
            floor, calibration = self.automatic.update(
                image, timestamp, objects or [], cut
            )
            # A fast pan can defeat frame-to-frame matching; the calibrated camera
            # still recognizes the same view and supplies its rotation motion.
            continuous = calibration.pop("continuous", False)
            camera_motion = calibration.pop("camera_motion", None)
            if motion is None and continuous:
                motion = camera_motion
                cut = False
        if cut:
            self.segment += 1
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
        self.observations = {}
        self.observation_pixels = {}
        self.spans = TeamSpans()
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
        color = self.sample(crop, self.background(image, box))
        if self.centers is not None and self.vote(color) is None:
            supported, _ = clothing_sample(self, image, box)
            if supported is not None:
                return supported
        return color

    def background(self, image: NDArray[Any], box: list[float]) -> NDArray[Any]:
        """Sample outside strips to reject a consistent neighbouring court colour."""
        h, w = image.shape[:2]
        x, y, bw, bh = box
        strips = [
            image[
                max(0, int((y + bh * 0.18) * h)) : min(h, int((y + bh * 0.48) * h)),
                max(0, int(a * w)) : min(w, int(b * w)),
            ].reshape(-1, 3)
            for a, b in ((x - bw * 0.2, x), (x + bw, x + bw * 1.2))
        ]
        return self.np.concatenate(strips)

    def sample(
        self, crop: NDArray[Any], background: NDArray[Any] | None = None
    ) -> NDArray[Any] | None:
        """Separate shirt hues from white numbers and mixed torso pixels."""
        if crop.size < MIN_SHIRT_VALUES:
            return None
        crop = foreground(self, crop, background).reshape(-1, 1, 3)
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
        self.spans.close_all()
        self.votes.clear()
        self.seen.clear()
        self.confirmed.clear()
        self.observations.clear()
        self.observation_pixels.clear()

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
        """Accumulate a track's shirt votes; hold its team through unclear crops."""
        identity = obj["track_id"]
        self.transfer_confirmation(obj, color, timestamp)
        self.seen[identity] = timestamp
        self.spans.observe(identity, timestamp)
        obj.update(team="unknown", team_score=0.0, team_source="unknown")
        obj.pop("team_age_seconds", None)
        vote = self.vote(color)
        votes = self.votes.setdefault(identity, deque(maxlen=TEAM_VOTE_HISTORY))
        prior = self.confirmed.get(identity)
        uncertain = obj.get("identity_uncertain")
        if uncertain and vote is not None and (prior is None or prior[0] != vote[0]):
            self.confirmed.pop(identity, None)
            self.spans.boundary(identity, timestamp)
            votes.clear()
            return
        if vote is not None and not uncertain:
            votes.append((vote[0], vote[1], timestamp))
            self.spans.vote(identity, f"team_{'ab'[vote[0]]}", timestamp)
            if switched(votes):
                # Keep only the opposing run: earlier votes described another body.
                recent = list(votes)[-SWITCH_VOTES:]
                votes.clear()
                votes.extend(recent)
                self.spans.boundary(identity, timestamp, resume=recent[0][2])
            prior = self.tally(votes, timestamp)
            if prior is None:
                held = self.confirmed.pop(identity, None)
                if held is not None:
                    self.spans.boundary(identity, timestamp)
                    if held[0] != vote[0]:
                        obj["team_source"] = "shirt_conflict"
                return
            self.confirmed[identity] = prior
            self.spans.confirm(identity, f"team_{'ab'[prior[0]]}", prior[2], votes)
        if prior is None or (uncertain and timestamp - prior[2] > TEAM_HOLD_SECONDS):
            return
        if vote is not None and vote[0] != prior[0]:
            # A clear opposing shirt is usually right about its own frame; keep
            # the track's evidence, but do not display it over this crop.
            obj["team_source"] = "shirt_conflict"
            return
        age = max(0.0, timestamp - prior[2])
        obj.update(
            team=f"team_{'ab'[prior[0]]}",
            team_score=prior[1],
            team_source="shirt"
            if vote is not None and not uncertain
            else ("track_history"),
            team_age_seconds=round(age, 3),
        )

    def tally(self, votes: deque, timestamp: float) -> tuple[int, float, float] | None:
        """Return a decayed majority team, its purity and latest supporting vote."""
        weights = [0.0, 0.0]
        for winner, _, time in votes:
            weights[winner] += 0.5 ** ((timestamp - time) / TEAM_HALF_LIFE_SECONDS)
        winner = int(weights[1] > weights[0])
        share = weights[winner] / max(EPSILON, sum(weights))
        support = [v for v in votes if v[0] == winner]
        if len(support) < MIN_TEAM_VOTES or share < MIN_VOTE_SHARE:
            return None
        return winner, round(share, 3), support[-1][2]

    def transfer_confirmation(
        self, obj: dict, color: NDArray[Any] | None, timestamp: float
    ) -> None:
        """Keep earned shirt evidence when a provisional track is confirmed."""
        link = obj.get("identity_confirmation", {})
        source, target = link.get("from_track_id"), obj["track_id"]
        if (
            obj.get("identity_uncertain")
            or source == target
            or link.get("to_track_id") != target
        ):
            return
        prior = self.confirmed.get(source)
        vote = self.vote(color)
        if (
            prior is None
            or vote is None
            or prior[0] != vote[0]
            or timestamp - prior[2] > TEAM_HOLD_SECONDS
        ):
            return
        existing = self.confirmed.get(target)
        if (
            existing is not None
            and timestamp - existing[2] <= TEAM_HOLD_SECONDS
            and existing[0] != prior[0]
        ):
            return
        self.confirmed[target] = prior
        self.votes[target] = deque(self.votes.get(source, ()), maxlen=TEAM_VOTE_HISTORY)

    def update(
        self, image: NDArray[Any], objects: list[dict], timestamp: float = 0
    ) -> None:
        """Keep bounded shirt evidence through occlusion and brief detector gaps."""
        expired = [
            k for k, t in self.seen.items() if timestamp - t > TEAM_MEMORY_SECONDS
        ]
        for identity in expired:
            self.spans.close(identity)
            self.seen.pop(identity, None)
            self.votes.pop(identity, None)
            self.confirmed.pop(identity, None)
        samples = []
        self.observations = {}
        self.observation_pixels = {}
        for obj in objects:
            if obj["label"] != "player" or obj.get("estimated"):
                continue
            color, evidence, values, visible = self.shirt_sample(image, obj, objects)
            samples.append((obj, color))
            self.observations[obj["track_id"]] = color
            if visible >= MIN_VISIBLE:
                self.observation_pixels[obj["track_id"]] = values[
                    :: max(1, math.ceil(len(values) / OPENING_PIXEL_SAMPLES))
                ].copy()
            obj["team_evidence"] = evidence
            # A brief guard freeze is not a new identity. Keep its earned team
            # evidence, but discard it when the guard confirms a body handoff.
            if obj.get("identity_issue") == "body_change":
                self.confirmed.pop(obj["track_id"], None)
                self.votes.pop(obj["track_id"], None)
                self.spans.boundary(obj["track_id"], timestamp)
        self.learn(samples)
        for obj, color in samples:
            self.assign(obj, color, timestamp)

    def shirt_sample(self, image: NDArray[Any], obj: dict, objects: list) -> tuple:
        """Separate visible shirts from court pixels beside overlapping bodies."""
        others = [o["observed_bbox"] for o in objects if o is not obj]
        color, evidence = clothing_sample(self, image, obj["observed_bbox"], others)
        # Preserve the established whole-shirt descriptor on clear torsos;
        # palette pixels supplement it when skin/trim obscures its vote.
        values, visible = clothing_pixels(self, image, obj["observed_bbox"], others)
        original = self.observe(image, obj["observed_bbox"])
        original_vote, masked_vote = self.vote(original), self.vote(color)
        if visible < 1 and original_vote is not None and values.size:
            foreground = self.sample(
                values.reshape(-1, 1, 3),
                self.background(image, obj["observed_bbox"]),
            )
            foreground_vote = self.vote(foreground)
            if (
                visible >= MIN_VISIBLE
                and foreground_vote is not None
                and foreground_vote[0] == original_vote[0]
            ):
                color, evidence = foreground, "visible"
                masked_vote = foreground_vote
        if visible == 1 and original_vote is not None:
            color, evidence = original, "visible"
        elif (
            original_vote is not None
            and masked_vote is not None
            and original_vote[0] != masked_vote[0]
        ):
            # The remaining sliver may be court rather than this person's
            # shirt. Contradictory masked pixels must not erase clean history.
            color, evidence = None, "mixed"
        return color, evidence, values, visible

    def colors(self) -> list[list[int]] | None:
        """Expose the learned palette for the legend without naming actual clubs."""
        if self.centers is None:
            return None
        return self.cv.cvtColor(self.np.uint8([self.centers]), self.cv.COLOR_LAB2RGB)[
            0
        ].tolist()


def switched(votes: deque) -> bool:
    """Detect a sustained, unanimous run opposing the track's earlier majority.

    Compare with earlier votes rather than the displayed label: one or two
    opposing crops already withdraw the label, and the swap must still resolve.
    """
    history = list(votes)
    recent, earlier = history[-SWITCH_VOTES:], history[:-SWITCH_VOTES]
    if len(recent) < SWITCH_VOTES or len(earlier) < MIN_TEAM_VOTES:
        return False
    team = recent[0][0]
    opposing = sum(v[0] != team for v in earlier)
    return (
        all(v[0] == team for v in recent)
        and opposing * 2 > len(earlier)
        and recent[-1][2] - recent[0][2] >= SWITCH_SECONDS - EPSILON
    )


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
