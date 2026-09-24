"""Recover player identities after brief occlusions interrupt native tracking."""

from __future__ import annotations

from itertools import pairwise
import math
from typing import TYPE_CHECKING, Any

from . import clip_identity_evidence, clip_identity_fragments
from .clip_body_motion import BodyMotion
from .clip_halves import CourtHalves
from .clip_identity_guard import ContinuationGuard
from .clip_identity_pending import PendingIdentities
from .clip_signals import Teams, center, distance, modules, transform


if TYPE_CHECKING:
    from numpy.typing import NDArray

IDENTITY_GAP = 3.0
MOTION_HISTORY_SECONDS = 1.2
COURT_IDENTITY_GAP = 6.0
MAX_SHIRT_DISTANCE = 25
MIN_SIZE_RATIO = 0.65
MAX_SIZE_RATIO = 1.55
MIN_RECOVERY_MARGIN = 0.3
MAX_RECOVERY_COST = 1.0
MIN_COURT_SIZE_RATIO = 0.4
MAX_COURT_SIZE_RATIO = 2.5
MAX_TORSO_OVERLAP = 0.35
MIN_FOOT_SIZE_RATIO = 0.7
MAX_FOOT_SIZE_RATIO = 1.4
MIN_SWAP_HISTORY = 3
MIN_IMAGE_HISTORY = 2
IMAGE_VELOCITY_SAMPLES = 5
SHIRT_SEED_GAP = 0.32
MIN_SHIRT_SEED_SAMPLES = 3
MAX_SWAP_GAP = 0.3
MAX_SWAP_COST = 0.65
MAX_GUARDED_SWAP_COST = 0.85
GUARDED_MOTION_GROWTH = 0.4


def court_reference(camera: dict) -> tuple | None:
    """Keep metric association separate across cuts and calibration replacements."""
    evidence = camera.get("calibration", {})
    status = evidence.get("status", "unknown")
    if camera.get("floor") is None or status == "unknown":
        return None
    if status.startswith("automatic") and "reference_time" not in evidence:
        return None
    return (
        camera.get("segment", 0),
        status.startswith("automatic"),
        evidence.get("reference_time"),
    )


class IdentityMemory:
    """Keep a public identity through a uniquely supported short tracker restart.

    Shirt colour is only a rejection signal: nearby teammates must still have a
    separated spatial match. This is not biometric or jersey-number recognition.
    """

    def __init__(self, court: dict | None = None) -> None:
        """Create isolated history for one camera segment."""
        self.tracks: dict[str, dict] = {}
        self.next_ids = {"player": 0, "referee": 0}
        self.shirts = Teams()
        self.metric_matches: set[int] = set()
        self.visible_shirts: set[str] = set()
        self.body = BodyMotion()
        self.pending = PendingIdentities(self)
        self.guard = ContinuationGuard()
        self.halves = CourtHalves(
            (court or {}).get("length", 40), (court or {}).get("width", 20)
        )
        _, self.np = modules()

    def reset(self) -> None:
        """Never transfer identities across a camera cut."""
        self.tracks.clear()
        self.visible_shirts.clear()
        self.next_ids = {"player": 0, "referee": 0}
        self.guard = ContinuationGuard()
        self.halves = CourtHalves(self.halves.length, self.halves.width)
        self.body = BodyMotion()
        self.pending = PendingIdentities(self)

    def shirt_distance(self, a: NDArray[Any], b: NDArray[Any]) -> float:
        """Emphasize jersey chroma over changing illumination for court recovery."""
        return float(self.np.linalg.norm(a[1:] - b[1:]) + 0.15 * abs(a[0] - b[0]))

    def advance(
        self,
        timestamp: float,
        motion: NDArray[Any] | None,
        max_gap: float = IDENTITY_GAP,
    ) -> None:
        """Carry the last observed position through every intermediate camera warp."""
        self.tracks = {
            k: v for k, v in self.tracks.items() if timestamp - v["time"] <= max_gap
        }
        if motion is not None:
            for prior in self.tracks.values():
                prior["point"] = transform(prior["point"], motion) or prior["point"]
                prior["locations"] = [
                    (t, transform(point, motion) or point)
                    for t, point in prior["locations"]
                ]

    def cost(
        self,
        prior: dict,
        obj: dict,
        color: NDArray[Any] | None,
        time: float,
        court_key: object = None,
    ) -> float:
        """Require compatible size, shirt and local motion before reconnecting."""
        metric = self.metric(prior, obj, court_key)
        prior_color = (
            prior["color"]
            if metric or len(prior["colors"]) >= MIN_IMAGE_HISTORY
            else prior["last_color"]
        )
        if prior["label"] != obj["label"] or color is None or prior_color is None:
            return float("inf")
        ratio = obj["observed_bbox"][3] / max(1e-6, prior["height"])
        guarded = prior.get("public_id") in self.guard.conflicts
        shirt = (
            self.shirt_distance(prior_color, color)
            if metric or guarded
            else float(self.np.linalg.norm(prior_color - color))
        )
        size_ok = (
            MIN_COURT_SIZE_RATIO < ratio < MAX_COURT_SIZE_RATIO
            if metric
            else MIN_SIZE_RATIO < ratio < MAX_SIZE_RATIO
        )
        if not size_ok or shirt > MAX_SHIRT_DISTANCE:
            return float("inf")
        dt = time - prior["time"]
        if metric:
            dt = time - prior["court_time"]
            expected = [
                p + v * min(dt, 1.2)
                for p, v in zip(prior["court_xy"], prior["court_velocity"], strict=True)
            ]
            # Allow footpoint noise, but do not reconnect physically impossible moves.
            if (
                dt > COURT_IDENTITY_GAP
                or distance(prior["court_xy"], obj["court_xy_m"]) > 0.9 + 9 * dt
            ):
                return float("inf")
            radius = min(3.0, 0.7 + dt * 0.65)
            return (
                distance(expected, obj["court_xy_m"]) / radius
                + shirt / 100
                + self.halves.penalty(prior.get("public_id", ""), obj, court_key, time)
            )
        if dt > IDENTITY_GAP:
            return float("inf")
        expected = [
            p + v * min(dt, 0.5)
            for p, v in zip(prior["point"], prior["velocity"], strict=True)
        ]
        radius = max(0.025, min(0.1, prior["height"] * 0.45))
        if guarded:
            radius += prior["height"] * GUARDED_MOTION_GROWTH * min(dt, 1.0)
        error = distance(expected, center(obj["observed_bbox"])) / radius
        anchors = prior.get("body_prediction")
        if anchors is not None:
            x, y, w, h = obj["observed_bbox"]
            body_error = sum(
                distance(anchor, [x + w / 2, y + h * fraction])
                for anchor, fraction in zip(anchors, (0.32, 0.58), strict=True)
            ) / (2 * radius)
            error = 0.25 * error + 0.75 * body_error
        return error + shirt / 100

    @staticmethod
    def metric(prior: dict, obj: dict, court_key: object) -> bool:
        """Compare metres only within the same verified court reference."""
        return bool(
            court_key is not None
            and prior.get("court_key") == court_key
            and prior.get("court_xy") is not None
            and obj.get("court_xy_m") is not None
        )

    def continuation(
        self,
        prior: dict,
        obj: dict,
        color: NDArray[Any] | None,
        time: float,
        court_key: object,
    ) -> bool:
        """Reject gross jumps only when both boxes provide comparable footpoints."""
        dt = time - prior["time"]
        if not self.metric(prior, obj, court_key):
            return dt <= IDENTITY_GAP
        if dt > IDENTITY_GAP:
            return self.cost(prior, obj, color, time, court_key) < MAX_RECOVERY_COST
        # Bounding-box feet can jump when legs overlap. Only a much larger
        # displacement is enough to override a continuing native track.
        return self.possible(prior, obj, time, court_key)

    def recover(
        self,
        objects: list[dict],
        colors: list,
        timestamp: float,
        court_key: object = None,
        metric_colors: list | None = None,
    ) -> dict[int, str]:
        """Use mutual best matches with a margin, reserving all native continuations."""
        native = {v["native"]: k for k, v in self.tracks.items()}
        matched = {
            i: native[o["track_id"]]
            for i, o in enumerate(objects)
            if o["track_id"] in native
            and self.continuation(
                self.tracks[native[o["track_id"]]], o, colors[i], timestamp, court_key
            )
        }
        self.metric_matches = set()
        clip_identity_fragments.recover(
            self,
            matched,
            list(zip(objects, colors, strict=True)),
            time=timestamp,
            key=court_key,
        )
        self.correct_swaps(
            matched, objects, metric_colors or colors, timestamp, court_key
        )
        self.correct_image_swaps(matched, objects, colors, timestamp, court_key)
        # Keep well-supported image associations. Court recovery is a second
        # pass for genuinely lost tracks, rather than replacing successful links.
        for metric_key in dict.fromkeys([None, court_key]):
            available = [
                k
                for k in self.tracks
                if k not in matched.values() and k not in self.pending.provisional_ids()
            ]
            missing = [
                i
                for i, o in enumerate(objects)
                if i not in matched and (not o.get("issue") or metric_key is not None)
            ]
            if not available or not missing:
                continue
            sample_colors = (
                metric_colors
                if metric_key is not None and metric_colors is not None
                else colors
            )
            costs = self.np.array([
                [
                    self.cost(
                        self.tracks[k],
                        objects[i],
                        sample_colors[i],
                        timestamp,
                        metric_key,
                    )
                    if objects[i].get("recovery_target", k) == k
                    and self.possible(self.tracks[k], objects[i], timestamp, court_key)
                    else float("inf")
                    for i in missing
                ]
                for k in available
            ])
            for row, identity in enumerate(available):
                column = int(self.np.argmin(costs[row]))
                best = costs[row, column]
                others = [
                    *self.np.delete(costs[row], column),
                    *self.np.delete(costs[:, column], row),
                ]
                if best < MAX_RECOVERY_COST and all(
                    v - best >= MIN_RECOVERY_MARGIN for v in others
                ):
                    matched[missing[column]] = identity
                    if metric_key is not None:
                        self.metric_matches.add(missing[column])
        return matched

    def correct_image_swaps(
        self,
        matched: dict,
        objects: list,
        colors: list,
        timestamp: float,
        court_key: object,
    ) -> None:
        """Repair reciprocal native swaps even when overlap hides the feet/torso.

        A contaminated crop alone cannot move an identity: both current bodies
        must contradict their assigned histories and uniquely support each other
        through recent motion and stable shirt evidence.
        """
        proposals = {}
        established = {
            identity: self.tracks[identity]
            for identity in matched.values()
            if len(self.tracks[identity]["colors"]) >= MIN_IMAGE_HISTORY
            and timestamp - self.tracks[identity]["time"]
            <= (
                MOTION_HISTORY_SECONDS
                if identity in self.guard.conflicts
                else MAX_SWAP_GAP
            )
        }
        for index, identity in matched.items():
            prior, color = established.get(identity), colors[index]
            if prior is None or color is None or prior["color"] is None:
                continue
            if self.shirt_distance(prior["color"], color) <= MAX_SHIRT_DISTANCE:
                continue
            ranked = sorted(
                (self.cost(track, objects[index], color, timestamp), key)
                for key, track in established.items()
                if self.possible(track, objects[index], timestamp, court_key)
            )
            if (
                ranked
                and ranked[0][0]
                < (
                    MAX_GUARDED_SWAP_COST
                    if identity in self.guard.conflicts
                    else MAX_SWAP_COST
                )
                and (
                    len(ranked) == 1
                    or ranked[1][0] - ranked[0][0] >= MIN_RECOVERY_MARGIN
                )
            ):
                proposals[index] = ranked[0][1]
        owners = {identity: index for index, identity in matched.items()}
        original = matched.copy()
        for index, identity in proposals.items():
            other = owners.get(identity)
            if other is not None and proposals.get(other) == original[index]:
                matched[index] = identity
                self.metric_matches.discard(index)

    def correct_swaps(
        self,
        matched: dict,
        objects: list,
        colors: list,
        timestamp: float,
        court_key: object,
    ) -> None:
        """Correct only reciprocal swaps supported by both court motion and shirts."""
        proposals = {}
        for index, identity in matched.items():
            prior = self.tracks[identity]
            color = colors[index]
            if not self.metric(prior, objects[index], court_key) or color is None:
                continue
            if (
                len(prior["colors"]) < MIN_SWAP_HISTORY
                or prior["color"] is None
                or timestamp - prior["time"] > MAX_SWAP_GAP
            ):
                continue
            if self.shirt_distance(prior["color"], color) <= MAX_SHIRT_DISTANCE:
                continue
            ranked = sorted(
                (self.cost(track, objects[index], color, timestamp, court_key), key)
                for key, track in self.tracks.items()
                if self.metric(track, objects[index], court_key)
            )
            if (
                ranked
                and ranked[0][0] < MAX_SWAP_COST
                and (
                    len(ranked) == 1
                    or ranked[1][0] - ranked[0][0] >= MIN_RECOVERY_MARGIN
                )
            ):
                proposals[index] = ranked[0][1]
        owners = {identity: index for index, identity in matched.items()}
        original = matched.copy()
        for index, identity in proposals.items():
            other = owners.get(identity)
            if other is not None and proposals.get(other) == original[index]:
                matched[index] = identity
                self.metric_matches.add(index)

    def possible(
        self, prior: dict, obj: dict, timestamp: float, court_key: object
    ) -> bool:
        """Even image-space recovery must not link a gross metric teleport."""
        ratio = obj["observed_bbox"][3] / max(1e-6, prior["height"])
        if not MIN_FOOT_SIZE_RATIO < ratio < MAX_FOOT_SIZE_RATIO:
            return True
        return not self.metric(prior, obj, court_key) or distance(
            prior["court_xy"], obj["court_xy_m"]
        ) <= 3 + 15 * (timestamp - prior["court_time"])

    def update(
        self,
        objects: list[dict],
        image: NDArray[Any],
        timestamp: float,
        motion: NDArray[Any] | None,
        *,
        court_key: object = None,
    ) -> list[dict]:
        """Reconcile observed people only; never draw a person while they are hidden."""
        self.advance(
            timestamp,
            motion,
            COURT_IDENTITY_GAP if court_key is not None else IDENTITY_GAP,
        )
        self.predict_body(image, timestamp)
        colors, metric_colors = clip_identity_evidence.samples(self, image, objects)
        matched = self.associate(objects, colors, timestamp, court_key, metric_colors)
        self.pending.reconcile(
            objects, colors, matched, timestamp=timestamp, court_key=court_key
        )
        retained = []
        for index, obj in enumerate(objects):
            native = obj["track_id"]
            identity = matched.get(index)
            if obj.get("recovery_target") and identity != obj["recovery_target"]:
                continue
            obj.pop("recovery_target", None)
            retained.append(obj)
            prior = self.tracks.get(identity) if identity is not None else None
            if identity is None:
                self.next_ids[obj["label"]] += 1
                next_id = self.next_ids[obj["label"]]
                identity = (
                    native if native not in self.tracks else f"{native}~{next_id}"
                )
            self.pending.remember(native, identity)
            display_id = prior["display_id"] if prior else self.next_ids[obj["label"]]
            point = center(obj["observed_bbox"])
            velocity = [0.0, 0.0]
            court_velocity = [0.0, 0.0]
            color = metric_colors[index]
            history, seed, reliable = self.appearance_history(prior, color, timestamp)
            same_reference = bool(
                prior
                and court_key is not None
                and prior["court_key"] == court_key
                and prior["court_xy"] is not None
                and 0 <= timestamp - prior["court_time"] <= COURT_IDENTITY_GAP
            )
            measured_contact = reliable and obj.get("court_xy_m") is not None
            if prior and timestamp > prior["time"]:
                samples = [*prior["locations"], (timestamp, point)][
                    -IMAGE_VELOCITY_SAMPLES:
                ]
                velocities = [
                    [(b[k] - a[k]) / (end - start) for k in (0, 1)]
                    for (start, a), (end, b) in pairwise(samples)
                    if 0 < end - start <= IDENTITY_GAP
                ]
                if velocities:
                    velocity = self.np.median(velocities, axis=0).tolist()
                if same_reference and measured_contact:
                    dt = timestamp - prior["court_time"]
                    measured = [
                        (a - b) / dt
                        for a, b in zip(
                            obj["court_xy_m"], prior["court_xy"], strict=True
                        )
                    ]
                    scale = min(1.0, 9 / max(1e-6, math.hypot(*measured)))
                    weight = min(0.6, dt / 0.35)
                    court_velocity = [
                        (1 - weight) * v + weight * m * scale
                        for v, m in zip(prior["court_velocity"], measured, strict=True)
                    ]
            obj.update(
                track_id=identity,
                native_track_id=native,
                display_id=display_id,
                identity_source=(
                    "new_after_conflict"
                    if prior is None and identity != native
                    else "court_reconnected"
                    if index in self.metric_matches
                    else "reconnected"
                    if identity != native
                    else "tracker"
                ),
            )
            # A rejected/reassigned native number may belong to only one public ID.
            for other, state in self.tracks.items():
                if prior is None and other != identity and state["native"] == native:
                    state["native"] = None
            self.save_identity(
                identity,
                prior,
                obj,
                {
                    "public_id": identity,
                    "native": native,
                    "display_id": display_id,
                    "label": obj["label"],
                    "point": point,
                    "locations": [
                        (t, p)
                        for t, p in (prior["locations"] if prior else [])
                        if timestamp - t <= MOTION_HISTORY_SECONDS
                    ][-15:]
                    + (
                        [(timestamp, point)]
                        if obj.get("observation_source") != "targeted_detection"
                        else []
                    ),
                    "height": obj["observed_bbox"][3],
                    "width": obj["observed_bbox"][2],
                    "full_time": timestamp
                    if obj.get("observation_source") != "targeted_detection"
                    else prior["full_time"]
                    if prior
                    else timestamp,
                    "color": self.np.median(history, axis=0) if history else None,
                    "last_color": colors[index]
                    if colors[index] is not None
                    else (prior["last_color"] if prior else None),
                    "colors": history,
                    "shirt_seed": seed if not reliable else [],
                    "velocity": velocity,
                    "court_xy": (
                        obj.get("court_xy_m")
                        if measured_contact
                        else prior["court_xy"]
                        if same_reference
                        else None
                    ),
                    "court_key": court_key,
                    "court_time": timestamp
                    if measured_contact or not same_reference
                    else prior["court_time"],
                    "court_velocity": court_velocity
                    if measured_contact or not same_reference
                    else prior["court_velocity"],
                    "time": timestamp,
                },
            )
        self.halves.update(retained, timestamp, court_key)
        self.body.seed(retained, timestamp)
        return retained

    def appearance_history(
        self, prior: dict | None, color: NDArray[Any] | None, timestamp: float
    ) -> tuple[list, list, bool]:
        """Keep mature anchors fixed; correct immature samples only with evidence."""
        compatible = (
            not prior
            or prior["color"] is None
            or color is None
            or self.shirt_distance(prior["color"], color) <= MAX_SHIRT_DISTANCE
        )
        seed = self.shirt_seed(prior, color, timestamp, compatible)
        learned = len(seed) >= MIN_SHIRT_SEED_SAMPLES
        reliable = color is not None and (compatible or learned)
        history = (
            [value for _, value in seed[:-1]]
            if learned
            else list(prior["colors"])
            if prior
            else []
        )
        if reliable:
            history.append(color)
        return history[-7:], seed, reliable

    def shirt_seed(
        self,
        prior: dict | None,
        color: NDArray[Any] | None,
        timestamp: float,
        compatible: bool,
    ) -> list:
        """Require repeated clear evidence to replace a bad first sample."""
        if not prior or len(prior["colors"]) >= MIN_SHIRT_SEED_SAMPLES or compatible:
            return []
        seed = [
            (time, value)
            for time, value in prior.get("shirt_seed", [])
            if timestamp - time <= SHIRT_SEED_GAP
        ]
        if color is None:
            return seed
        if seed and self.shirt_distance(seed[-1][1], color) > MAX_SHIRT_DISTANCE:
            seed = []
        return [*seed, (timestamp, color)]

    def associate(
        self,
        objects: list,
        colors: list,
        timestamp: float,
        court_key: object,
        metric_colors: list,
    ) -> dict:
        """Evaluate continuity once, after tentative association and swap correction."""
        matched = self.recover(objects, colors, timestamp, court_key, metric_colors)
        before = dict(matched)
        self.guard.check(self, matched, objects, colors, timestamp)
        if matched != before:
            matched = self.recover(objects, colors, timestamp, court_key, metric_colors)
            for index, identity in matched.items():
                if identity != before.get(index):
                    objects[index].pop("identity_uncertain", None)
        return matched

    def save_identity(
        self, identity: str, prior: dict | None, obj: dict, state: dict
    ) -> None:
        """Do not refresh identity history with a body under investigation."""
        if obj.get("identity_uncertain") and prior is not None:
            return
        obj.pop("identity_uncertain", None)
        self.tracks[identity] = state

    def predict_body(self, image: NDArray[Any], timestamp: float) -> None:
        """Attach only this frame's visible-body motion to retained identities."""
        predictions = self.body.predict(image, timestamp)
        for identity, prior in self.tracks.items():
            prior["body_prediction"] = predictions.get(identity)

    @staticmethod
    def clear_torso(obj: dict, objects: list[dict]) -> bool:
        """Do not learn another person's shirt from an overlapping torso crop."""
        x, y, w, h = obj["observed_bbox"]
        left, top, right, bottom = x + 0.2 * w, y + 0.18 * h, x + 0.8 * w, y + 0.48 * h
        area = max(1e-9, (right - left) * (bottom - top))
        for other in objects:
            if other is obj:
                continue
            a, b, c, d = other["observed_bbox"]
            overlap = max(0, min(right, a + c) - max(left, a)) * max(
                0, min(bottom, b + d) - max(top, b)
            )
            if overlap / area > MAX_TORSO_OVERLAP:
                return False
        return True
