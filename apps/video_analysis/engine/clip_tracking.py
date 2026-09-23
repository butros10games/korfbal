"""Class-separated people tracking and a separate, uncertainty-aware ball path."""

from __future__ import annotations

from collections import Counter
import importlib
import math
from operator import itemgetter
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

from .clip_contract import ClipOptions
from .clip_identity import IDENTITY_GAP, IdentityMemory
from .clip_signals import center, distance, floor_position, modules, transform


if TYPE_CHECKING:
    from numpy.typing import NDArray

MAX_PLAYER_SPEED = 1.5
MAX_FLOOR_SPEED = 15
SMOOTHING_GAP = 0.3
BALL_GAP = 1.5
ACTIVE_BALL_GAP = 0.6
MIN_AREA_RATIO = 0.2
MAX_AREA_RATIO = 5
INVALID_COST = 1e6
MIN_BALL_HITS = 3
MIN_ACTIVE_SCORE = 0.6
MIN_ACTIVE_MARGIN = 0.12
MIN_BALL_RECOVERY_MARGIN = 0.02


class Tracker(Protocol):
    """The pinned runtime's association capability, without a web dependency."""

    def update(self, detections: object, image: NDArray[Any]) -> NDArray[Any]:
        """Return aligned track rows for one consecutive frame."""
        ...


class FrameMotion:
    """Share frame-only sparse optical flow between the two class trackers."""

    def __init__(self, estimator: object) -> None:
        """Keep one temporal estimator and one result for each explicit frame."""
        self.estimator = cast("Any", estimator)
        self.method = self.estimator.method
        self.begin_frame()

    def begin_frame(self) -> None:
        """Invalidate explicitly, including when a decoder reuses its image buffer."""
        self.ready = False
        self.result = None
        self.error: Exception | None = None

    def apply(self, image: object, _detections: object = None) -> object:
        """Compute once; both classes receive the same motion or fallback error."""
        if not self.ready:
            self.ready = True
            try:
                # sparseOptFlow uses only the image, not class-specific boxes.
                self.result = self.estimator.apply(image)
            except Exception as error:
                self.error = error
                raise
        if self.error:
            raise self.error
        return self.result

    def reset_params(self) -> None:
        """Honor the upstream reset contract as well as per-frame caching."""
        self.estimator.reset_params()
        self.begin_frame()


class People:
    """Use the installed ByteTrack/BoT-SORT implementation independently per class."""

    def __init__(self, options: ClipOptions) -> None:
        """Create independent per-class identity and motion state."""
        self.options = options
        self.trackers: dict[str, Tracker] = {}
        self.motion: FrameMotion | None = None
        self.previous = {}
        self.generations = Counter()
        self.segment = 0
        self.identities = IdentityMemory()

    def reset(self, segment: int) -> None:
        """Discard identities and motion history across a camera cut."""
        self.identities.reset()
        self.trackers.clear()
        self.motion = None
        self.previous.clear()
        self.generations.clear()
        self.segment = segment

    def tracker(self, label: str) -> Tracker:
        """Construct the selected pinned Ultralytics tracker."""
        if label not in self.trackers:
            utils = importlib.import_module("ultralytics.utils")
            name = self.options.tracker
            module = importlib.import_module(
                "ultralytics.trackers."
                + ("bot_sort" if name == "botsort" else "byte_tracker")
            )
            factory = module.BOTSORT if name == "botsort" else module.BYTETracker
            config = utils.YAML.load(utils.ROOT / "cfg" / "trackers" / f"{name}.yaml")
            config.update(
                track_high_thresh=self.options.confidence,
                track_low_thresh=0.1,
                new_track_thresh=self.options.confidence,
                track_buffer=max(1, round(self.options.fps * IDENTITY_GAP)),
                with_reid=False,
            )
            tracker = factory(SimpleNamespace(**config))
            if name == "botsort" and config["gmc_method"] == "sparseOptFlow":
                if self.motion is None:
                    self.motion = FrameMotion(tracker.gmc)
                tracker.gmc = self.motion
            self.trackers[label] = cast("Tracker", tracker)
        return self.trackers[label]

    def advance(self, motion: NDArray[Any] | None) -> None:
        """Advance camera motion for visible and temporarily hidden people."""
        if motion is not None:
            for previous in self.previous.values():
                previous["projected"] = (
                    transform(previous["projected"], motion) or previous["projected"]
                )

    def update(
        self, raw: object, image: NDArray[Any], timestamp: float, camera: dict
    ) -> list[dict]:
        """Retain observed boxes, refusing physically implausible identity links."""
        result = cast("Any", raw)
        # Constructors reset a shared identity counter; create both before tracking.
        for label in ("player", "referee"):
            self.tracker(label)
        if self.motion:
            self.motion.begin_frame()
        _, np = modules()
        h, w = image.shape[:2]
        objects = []
        self.advance(camera["motion"])
        boxes = result.boxes.cpu().numpy()
        for label in ("player", "referee"):
            classes = [
                k
                for k, v in result.names.items()
                if v == label or (label == "player" and v == "person")
            ]
            detections = boxes[np.isin(boxes.cls, classes)]
            tracks = self.tracker(label).update(detections, image)
            for row in tracks:
                native_id, index = int(row[4]), int(row[-1])
                raw = detections.xyxy[index].tolist()
                x1, y1, x2, y2 = [
                    max(0.0, min(1.0, v / scale))
                    for v, scale in zip(raw, (w, h, w, h), strict=True)
                ]
                if x2 <= x1 or y2 <= y1:
                    continue
                box = [x1, y1, x2 - x1, y2 - y1]
                key = (label, native_id)
                previous = self.previous.get(key)
                position = floor_position(box, camera["floor"], self.options.court)
                issue = None
                if previous:
                    dt = timestamp - previous["time"]
                    projected = previous["projected"]
                    speed = (
                        distance(projected, center(box)) / dt
                        if projected and dt > 0
                        else 0
                    )
                    floor_speed = (
                        distance(position, previous["court_xy_m"]) / dt
                        if position
                        and previous["court_xy_m"]
                        and dt > 0
                        and not camera.get("calibration", {}).get("estimated")
                        else 0
                    )
                    if speed > MAX_PLAYER_SPEED or floor_speed > MAX_FLOOR_SPEED:
                        # Retain the detection while refusing the identity link.
                        self.generations[key] += 1
                        previous = None
                        issue = "implausible_motion_identity_reset"
                smoothed = box
                if previous and timestamp - previous["time"] <= SMOOTHING_GAP:
                    smoothed = [
                        0.8 * a + 0.2 * b
                        for a, b in zip(box, previous["bbox"], strict=True)
                    ]
                obj = {
                    "label": label,
                    "track_id": f"s{self.segment}-{label}-{native_id}-"
                    f"{self.generations[key]}",
                    "bbox": smoothed,
                    "observed_bbox": box,
                    "confidence": float(detections.conf[index]),
                    "estimated": False,
                    "court_xy_m": position,
                    "team": "unknown",
                }
                if issue:
                    obj["issue"] = issue
                objects.append(obj)
                self.previous[key] = dict(obj, time=timestamp, projected=center(box))
        # Retain short gaps for association without drawing invisible people.
        self.previous = {
            k: v
            for k, v in self.previous.items()
            if timestamp - v["time"] <= IDENTITY_GAP
        }
        return self.identities.update(objects, image, timestamp, camera["motion"])


class Balls:
    """Associate tiny fast objects, then select an active candidate or abstain.

    Scores are heuristic evidence scores, not calibrated probabilities. Every ball
    detection survives in the output, including stationary spare-ball candidates.
    """

    def __init__(self) -> None:
        """Start a bounded collection of ball candidates without an active guess."""
        self.tracks = {}
        self.next_id = 0
        self.active = None
        self.active_seen = -math.inf
        self.segment = 0

    def reset(self, segment: int) -> None:
        """Discard identities and motion history across a camera cut."""
        self.tracks.clear()
        self.active = None
        self.active_seen = -math.inf
        self.segment = segment

    def associate(
        self, detections: list[dict], timestamp: float, motion: NDArray[Any] | None
    ) -> dict[int, str]:
        """Use one-to-one assignment with motion and scale gates for tiny objects."""
        _, np = modules()
        self.tracks = {
            k: v for k, v in self.tracks.items() if timestamp - v["time"] <= BALL_GAP
        }
        if motion is not None:
            for prior in self.tracks.values():
                prior["projected"] = (
                    transform(prior["projected"], motion) or prior["projected"]
                )
        identities = list(self.tracks)
        costs = np.full((len(identities), len(detections)), INVALID_COST)
        for i, identity in enumerate(identities):
            prior = self.tracks[identity]
            dt = timestamp - prior["time"]
            projected = prior["projected"]
            expected = [
                p + v * min(dt, 0.3)
                for p, v in zip(
                    projected or center(prior["bbox"]), prior["velocity"], strict=True
                )
            ]
            for j, obj in enumerate(detections):
                d = distance(expected, center(obj["bbox"]))
                ratio = (obj["bbox"][2] * obj["bbox"][3]) / max(
                    1e-9, prior["bbox"][2] * prior["bbox"][3]
                )
                if (
                    d < min(0.18, 0.025 + dt * 1.2)
                    and MIN_AREA_RATIO < ratio < MAX_AREA_RATIO
                ):
                    costs[i, j] = d + abs(math.log(ratio)) * 0.005
        return self.match(costs, identities, timestamp)

    def match(
        self, costs: NDArray[Any], identities: list[str], timestamp: float
    ) -> dict[int, str]:
        """Reserve visible balls before recovering older lost candidates."""
        _, np = modules()
        assignment = importlib.import_module("scipy.optimize").linear_sum_assignment
        matched = {}
        for recovering in (False, True):
            rows = [
                i
                for i, key in enumerate(identities)
                if (timestamp - self.tracks[key]["time"] > SMOOTHING_GAP) == recovering
            ]
            columns = [j for j in range(costs.shape[1]) if j not in matched]
            if not rows or not columns:
                continue
            subset = costs[np.ix_(rows, columns)]
            sources, targets = assignment(subset)
            for i, j in zip(sources, targets, strict=True):
                if subset[i, j] < INVALID_COST and (
                    not recovering or self.separated(subset, int(i), int(j))
                ):
                    matched[columns[j]] = identities[rows[i]]
        return matched

    @staticmethod
    def separated(costs: NDArray[Any], row: int, column: int) -> bool:
        """Do not guess which ball returned when another link is equally plausible."""
        _, np = modules()
        alternatives = [
            *np.delete(costs[row], column),
            *np.delete(costs[:, column], row),
        ]
        return all(
            value - costs[row, column] >= MIN_BALL_RECOVERY_MARGIN
            for value in alternatives
        )

    def update(
        self,
        detections: list[dict],
        people: list[dict],
        timestamp: float,
        motion: NDArray[Any] | None,
    ) -> tuple[list[dict], dict]:
        """Associate candidates and select a separated active hypothesis."""
        matched = self.associate(detections, timestamp, motion)
        objects = []
        candidates = []
        for index, detection in enumerate(detections):
            identity = matched.get(index)
            if identity is None:
                self.next_id += 1
                identity = f"s{self.segment}-ball-{self.next_id}"
            prior = self.tracks.get(identity)
            point = center(detection["bbox"])
            velocity = [0.0, 0.0]
            motion_evidence = 0.0
            if prior:
                projected = prior["projected"] if motion is not None else None
                dt = timestamp - prior["time"]
                if projected is not None and dt > 0:
                    velocity = [
                        (a - b) / dt for a, b in zip(point, projected, strict=True)
                    ]
                    motion_evidence = min(1.0, math.hypot(*velocity) / 0.12)
            proximity = 0.0
            for person in people:
                x, y, w, h = person["observed_bbox"]
                # Distance to the person's visible box, scaled by their image height.
                delta = math.hypot(
                    max(x - point[0], 0, point[0] - x - w),
                    max(y - point[1], 0, point[1] - y - h),
                )
                proximity = max(proximity, 0.0, 1 - delta / max(0.03, h * 0.75))
            hits = (prior["hits"] if prior else 0) + 1
            score = (
                0.2 * detection["confidence"]
                + 0.45 * proximity
                + 0.2 * motion_evidence
                + (0.15 if identity == self.active else 0)
            )
            obj = dict(
                detection,
                track_id=identity,
                estimated=False,
                observed_bbox=detection["bbox"],
                active_score=round(score, 3),
                role="unknown",
                court_xy_m=None,
            )
            self.tracks[identity] = dict(
                obj, velocity=velocity, time=timestamp, hits=hits, projected=point
            )
            objects.append(obj)
            if hits >= MIN_BALL_HITS:
                candidates.append((score, obj))
        candidates.sort(key=itemgetter(0), reverse=True)
        active = {"status": "unknown", "track_id": None, "score": None}
        if candidates:
            score, best = candidates[0]
            margin = score - candidates[1][0] if len(candidates) > 1 else score
            switching_during_gap = (
                self.active is not None
                and best["track_id"] != self.active
                and timestamp - self.active_seen < ACTIVE_BALL_GAP
            )
            if (
                score >= MIN_ACTIVE_SCORE
                and margin >= MIN_ACTIVE_MARGIN
                and not switching_during_gap
            ):
                self.active = best["track_id"]
                self.active_seen = timestamp
                best["role"] = "active_candidate"
                active = {
                    "status": "observed",
                    "track_id": self.active,
                    "score": round(score, 3),
                }
        if timestamp - self.active_seen > ACTIVE_BALL_GAP:
            self.active = None
        return objects, active
