"""Spend bounded extra inference on recently lost, unambiguous player tracks.

Search predictions never become observations. Only a detected box with a unique
appearance/motion match can enter the existing identity association.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import time
from typing import TYPE_CHECKING, Any, cast

from .clip_contract import ClipOptions
from .clip_identity import (
    MAX_RECOVERY_COST,
    MIN_RECOVERY_MARGIN,
    IdentityMemory,
    court_reference,
)
from .clip_signals import distance, floor_position, transform


if TYPE_CHECKING:
    from numpy.typing import NDArray


DUPLICATE_IOU = 0.4
BORDER_PIXELS = 2
IMAGE_MARGIN = 0.02
MIN_CROP_SIZE = 32
MAX_SEARCH_GAP = 1.2
RETRY_SECONDS = 0.24
CROP_SIZE = 640
MIN_HISTORY = 3
MIN_BODY_MOVEMENT = 0.12
INFERENCE_FRACTION = 0.2


def overlap(a: list, b: list) -> float:
    """Intersection over union for normalized xywh observations."""
    area = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(
        0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    )
    return area / max(1e-9, a[2] * a[3] + b[2] * b[3] - area)


@dataclass(frozen=True)
class RecoveryFrame:
    """One decoded frame and its bounded inference capability."""

    raw: object
    image: NDArray[Any]
    timestamp: float
    camera: dict
    detector: object
    inference_seconds: float
    stopped: Callable[[], bool]
    other_seconds: float = 0


class PlayerRecovery:
    """Keep one search budget for a run; camera cuts only clear track retries."""

    def __init__(self, options: ClipOptions, *, crops: bool = False) -> None:
        """Start bounded counters and retry history for one run."""
        self.options = options
        self.crops = crops
        self.attempted: dict[str, float] = {}
        self.frames = 0
        self.calls = 0
        self.seconds = 0.0
        self.added = 0
        self.full_frame_added = 0
        self.serial = 0

    def snapshot(self) -> dict:
        """Expose cost and observation counts, never label them accuracy."""
        return {
            "version": 1,
            "crop_search_enabled": self.crops,
            "frames": self.frames,
            "crop_calls": self.calls,
            "crop_seconds": round(self.seconds, 3),
            "crop_observations": self.added,
            "full_frame_observations": self.full_frame_added,
            "max_search_gap_seconds": MAX_SEARCH_GAP,
            "max_inference_fraction": INFERENCE_FRACTION,
        }

    def recover(
        self,
        memory: IdentityMemory,
        observed: list[dict],
        frame: RecoveryFrame,
    ) -> list[dict]:
        """Recover unused full-frame evidence first, then at most one small crop.

        Memory has already been advanced into this camera frame by People.
        The native tracker is updated only once; no prediction enters its input.
        """
        raw, image, timestamp = frame.raw, frame.image, frame.timestamp
        camera, stopped = frame.camera, frame.stopped
        self.frames += 1
        if camera.get("cut"):
            self.attempted.clear()
        self.attempted = {k: v for k, v in self.attempted.items() if k in memory.tracks}
        if not memory.tracks or stopped():
            return []
        key = court_reference(camera)
        colors = [memory.shirts.observe(image, o["observed_bbox"]) for o in observed]
        clear = [
            c if memory.clear_torso(o, observed) else None
            for o, c in zip(observed, colors, strict=True)
        ]
        reserved = set(memory.recover(observed, colors, timestamp, key, clear).values())
        missing = {
            k: v
            for k, v in memory.tracks.items()
            if k not in reserved
            and v["label"] == "player"
            and len(v["colors"]) >= MIN_HISTORY
            and 0 < timestamp - v["full_time"] <= MAX_SEARCH_GAP
            and self.searchable(v, camera)
        }
        if not missing:
            return []
        candidates = self.objects(raw, camera)
        # A box ignored by native association may still uniquely match a lost ID.
        recovered = self.accept(memory, missing, candidates, observed, frame)
        for obj in recovered:
            obj["observation_source"] = "full_frame_recovery"
            missing.pop(obj["recovery_target"])
        self.full_frame_added += len(recovered)
        if (
            not missing
            or not self.crops
            or self.calls >= self.frames // 3
            or self.seconds + frame.other_seconds
            > frame.inference_seconds * INFERENCE_FRACTION
            or stopped()
        ):
            return recovered
        return recovered + self.search(
            memory, missing, observed + recovered + candidates, frame
        )

    def searchable(self, prior: dict, camera: dict) -> bool:
        """Do not repeatedly magnify static posters or off-court people."""
        xy = prior["court_xy"]
        if (
            xy is not None
            and self.options.court
            and prior["court_key"] == court_reference(camera)
            and not camera.get("calibration", {}).get("estimated")
        ):
            return all(
                0 <= value <= self.options.court[axis]
                for value, axis in zip(xy, ("length", "width"), strict=True)
            )
        locations = prior["locations"]
        return (
            len(locations) >= MIN_HISTORY
            and max(distance(a, b) for _, a in locations for _, b in locations)
            > prior["height"] * MIN_BODY_MOVEMENT
        )

    def search(
        self,
        memory: IdentityMemory,
        missing: dict,
        occupied: list[dict],
        frame: RecoveryFrame,
    ) -> list[dict]:
        """Try one eligible crop, charging export and inference to the same budget."""
        image, timestamp = frame.image, frame.timestamp
        camera, stopped = frame.camera, frame.stopped
        for identity in sorted(missing, key=lambda k: self.attempted.get(k, -math.inf)):
            if timestamp - self.attempted.get(identity, -math.inf) < RETRY_SECONDS:
                continue
            region = self.region(
                memory, missing[identity], timestamp, camera, image.shape
            )
            if region is None:
                continue
            left, top, right, bottom = region
            self.attempted[identity] = timestamp
            self.calls += 1
            started = time.monotonic()
            try:
                # Square padding fixes the export shape; never shrink source pixels.
                patch = memory.np.full(
                    (max(right - left, bottom - top),) * 2 + (3,),
                    114,
                    dtype=image.dtype,
                )
                patch[: bottom - top, : right - left] = image[top:bottom, left:right]
                result = cast("Any", frame.detector).predict(
                    patch,
                    device="cpu",
                    imgsz=CROP_SIZE,
                    conf=self.options.confidence,
                    max_det=24,
                    verbose=False,
                )[0]
            finally:
                self.seconds += time.monotonic() - started
            if stopped():
                return []
            extras = self.objects(result, camera, offset=region, shape=image.shape)
            # Include all full-frame boxes in duplicate checks, even untracked ones.
            accepted = self.accept(
                memory,
                missing,
                extras,
                occupied,
                frame,
            )
            for obj in accepted:
                obj["observation_source"] = "targeted_detection"
            self.added += len(accepted)
            return accepted
        return []

    def objects(
        self,
        result: object,
        camera: dict,
        *,
        offset: tuple | None = None,
        shape: tuple | None = None,
    ) -> list[dict]:
        """Restore crop coordinates once and exclude padding/clipped bodies."""
        output = []
        result = cast("Any", result)
        rows = result.boxes.cpu().numpy()
        for xyxy, cls, score in zip(rows.xyxy, rows.cls, rows.conf, strict=True):
            if (
                result.names[int(cls)] not in {"player", "person"}
                or score < self.options.confidence
            ):
                continue
            a, b, c, d = (float(v) for v in xyxy)
            h, w = shape[:2] if shape is not None else result.orig_shape
            if offset is not None:
                left, top, right, bottom = offset
                if (
                    a < BORDER_PIXELS
                    or b < BORDER_PIXELS
                    or c > right - left - BORDER_PIXELS
                    or d > bottom - top - BORDER_PIXELS
                ):
                    continue
                a, b, c, d = a + left, b + top, c + left, d + top
            box = [a / w, b / h, (c - a) / w, (d - b) / h]
            if box[2] <= 0 or box[3] <= 0:
                continue
            output.append({
                "label": "player",
                "bbox": box,
                "observed_bbox": box,
                "confidence": float(score),
                "estimated": False,
                "court_xy_m": floor_position(
                    box, camera.get("floor"), self.options.court
                ),
                "team": "unknown",
            })
        return output

    def accept(
        self,
        memory: IdentityMemory,
        missing: dict,
        candidates: list[dict],
        occupied: list[dict],
        frame: RecoveryFrame,
    ) -> list[dict]:
        """Require mutual unique appearance/motion agreement and no duplicate box."""
        image, timestamp = frame.image, frame.timestamp
        key = court_reference(frame.camera)
        proposals = []
        for obj in candidates:
            box = obj["observed_bbox"]
            if any(overlap(box, o["observed_bbox"]) > DUPLICATE_IOU for o in occupied):
                continue
            if not memory.clear_torso(obj, occupied + candidates):
                continue
            color = memory.shirts.observe(image, box)
            # Also compare occupied identities: a lost ID cannot steal their body.
            scores = sorted(
                (memory.cost(prior, obj, color, timestamp, key), identity)
                for identity, prior in memory.tracks.items()
                if memory.possible(prior, obj, timestamp, key)
            )
            if not scores or scores[0][0] >= MAX_RECOVERY_COST:
                continue
            cost, identity = scores[0]
            if identity not in missing or (
                len(scores) > 1 and scores[1][0] - cost < MIN_RECOVERY_MARGIN
            ):
                continue
            proposals.append((cost, identity, obj))
        accepted = []
        for cost, identity, obj in proposals:
            if any(
                other is not obj
                and owner == identity
                and value - cost < MIN_RECOVERY_MARGIN
                for value, owner, other in proposals
            ):
                continue
            self.serial += 1
            obj.update(track_id=f"recovery-{self.serial}", recovery_target=identity)
            accepted.append(obj)
        return accepted

    @staticmethod
    def region(
        memory: IdentityMemory,
        prior: dict,
        timestamp: float,
        camera: dict,
        shape: tuple,
    ) -> tuple | None:
        """Project a bounded envelope, using reliable court feet when possible."""
        dt = timestamp - prior["time"]
        point = [
            p + v * min(dt, 0.5)
            for p, v in zip(prior["point"], prior["velocity"], strict=True)
        ]
        key = court_reference(camera)
        if (
            key is not None
            and key == prior["court_key"]
            and prior["court_xy"] is not None
            and not camera.get("calibration", {}).get("estimated")
        ):
            elapsed = timestamp - prior["court_time"]
            feet = [
                p + v * min(elapsed, 1.2)
                for p, v in zip(prior["court_xy"], prior["court_velocity"], strict=True)
            ]
            try:
                mapped = transform(feet, memory.np.linalg.inv(camera["floor"]))
            except memory.np.linalg.LinAlgError:
                mapped = None
            if mapped is not None:
                point = [mapped[0], mapped[1] - prior["height"] / 2]
        if not all(IMAGE_MARGIN < v < 1 - IMAGE_MARGIN for v in point):
            return None
        h, w = shape[:2]
        uncertainty = 1 + min(timestamp - prior["full_time"], 1) * 0.5
        side = math.ceil(
            max(prior["width"] * w * 3, prior["height"] * h * 1.7) * uncertainty
        )
        if not MIN_CROP_SIZE <= side <= CROP_SIZE:
            return None
        left = max(0, min(w - side, round(point[0] * w - side / 2)))
        top = max(0, min(h - side, round(point[1] * h - side / 2)))
        return left, top, min(w, left + side), min(h, top + side)
