"""Bounded automatic reference preparation inside the clip's original deadline."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import importlib
import time
from typing import TYPE_CHECKING, Any, cast
import warnings

from . import clip_basket_reference, clip_penalty_area
from .clip_auto_court import estimate
from .clip_inference import CPU_THREADS
from .clip_signals import modules
from .clips import static_objects


if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray

    from .clip_auto_court import AutoCourt
    from .clips import ClipRun

MAX_SAMPLES = 96
MAX_SECONDS = 120
SAMPLE_INTERVAL = 0.8
WINDOW_SAMPLES = 21
MIN_IMAGES = 5
MIN_PIXEL_OBSERVATIONS = 2
REFERENCE_TIME_TOLERANCE = 0.1
PROGRESS_SECONDS = 5


class TemporalReference:
    """Align visible observations and remove moving people before fitting markings."""

    def __init__(
        self, mapping: AutoCourt, stopped: Callable[[], bool] | None = None
    ) -> None:
        """Bound memory to one short camera view."""
        self.mapping = mapping
        self.stopped = stopped or (lambda: False)
        self.samples: deque = deque(maxlen=WINDOW_SAMPLES)

    def add(self, image: NDArray[Any], timestamp: float, objects: list) -> None:
        """Separate unmatched camera views instead of blending across cuts."""
        cv, _ = modules()
        factor = min(1, 1280 / image.shape[1], 720 / image.shape[0])
        image = cv.resize(image, None, fx=factor, fy=factor)
        features = self.mapping.matcher.features(image, [o["bbox"] for o in objects])
        if (
            self.samples
            and self.mapping.matcher.register(features, self.samples[-1]["features"])
            is None
        ):
            self.prepare()
            self.samples.clear()
        self.samples.append({
            "image": image,
            "time": timestamp,
            "objects": objects,
            "features": features,
        })

    def prepare(self) -> bool:
        """Try a few real viewpoints with the same visibility requirements."""
        if len(self.samples) < MIN_IMAGES:
            return False
        indices = dict.fromkeys([
            len(self.samples) // 2,
            len(self.samples) * 2 // 3,
            len(self.samples) // 3,
        ])
        for index in indices:
            if self.stopped():
                return False
            if self.prepare_target(self.samples[index]):
                self.samples.clear()
                return True
        return False

    def prepare_target(self, target: dict) -> bool:
        """Fit measurements at one real frame's geometry; never synthesize pixels."""
        cv, np = modules()
        height, width = target["image"].shape[:2]
        scale = np.diag([float(width), float(height), 1.0])
        images, masks, basket_votes = [], [], []
        for sample in self.samples:
            if self.stopped():
                return False
            match = self.mapping.matcher.register(
                sample["features"], target["features"]
            )
            if match is None:
                continue
            basket_votes.extend(
                clip_basket_reference.observations(
                    sample["objects"], match[0], sample["time"]
                )
            )
            warp = scale @ match[0] @ np.linalg.inv(scale)
            image = cv.warpPerspective(sample["image"], warp, (width, height)).astype(
                np.float32
            )
            mask = cv.resize(
                sample["features"][1], (width, height), interpolation=cv.INTER_NEAREST
            )
            mask = cv.warpPerspective(
                mask, warp, (width, height), flags=cv.INTER_NEAREST
            )
            image[mask == 0] = np.nan
            images.append(image)
            masks.append(mask > 0)
        if len(images) < MIN_IMAGES:
            return False
        # Pixels hidden in every input deliberately stay invalid, not inpainted.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message="All-NaN slice encountered", category=RuntimeWarning
            )
            median = np.nanmedian(np.array(images), axis=0)
        valid = np.uint8(np.sum(masks, axis=0) >= MIN_PIXEL_OBSERVATIONS) * 255
        median[valid == 0] = 0
        median = np.nan_to_num(median).astype(np.uint8)
        objects = clip_basket_reference.combine(basket_votes)
        found = clip_penalty_area.estimate(median, objects, self.mapping.court, valid)
        if found is None:
            return False
        floor, evidence = found
        evidence.update(
            reference_images=len(images),
            reference_time=target["time"],
            basket_observations=[o["observations"] for o in objects],
        )
        return self.mapping.references.add(
            target["image"],
            target["time"],
            target["objects"],
            (floor, evidence),
            observation=(median, valid),
        )


def prepare(run: ClipRun, video: Path, model: object) -> None:
    """Find clear references on either side of playback frames without another job."""
    mapping = run.camera.automatic
    if mapping is None:
        return
    cv, _ = modules()
    capture = cv.VideoCapture(
        str(video), cv.CAP_FFMPEG, [cv.CAP_PROP_N_THREADS, CPU_THREADS]
    )
    started = time.monotonic()
    temporal = TemporalReference(
        mapping, lambda: run.stopped() or time.monotonic() - started > MAX_SECONDS
    )
    interval = max(SAMPLE_INTERVAL, run.options.duration / MAX_SAMPLES)
    sampled = 0
    try:
        for index in range(MAX_SAMPLES):
            timestamp = run.options.start + interval * index
            if (
                timestamp >= run.options.start + run.options.duration
                or time.monotonic() - started > MAX_SECONDS
                or run.stopped()
            ):
                break
            snapshot = sample(run, capture, model, timestamp)
            if snapshot is None:
                continue
            sampled += 1
            actual, image, objects = snapshot
            run.record["message"] = (
                f"Preparing court references ({sampled} frames checked)"
            )
            if time.monotonic() - run.last_publish >= PROGRESS_SECONDS:
                run.publish()
            if mapping.references.find(image, actual, objects):
                temporal.prepare()
                temporal.samples.clear()
                continue
            found = estimate(image, objects, mapping.court)
            if found:
                mapping.references.add(image, actual, objects, found)
                temporal.prepare()
                temporal.samples.clear()
            else:
                temporal.add(image, actual, objects)
                if len(temporal.samples) == WINDOW_SAMPLES:
                    temporal.prepare()
                    temporal.samples.clear()
        if not run.stopped() and time.monotonic() - started <= MAX_SECONDS:
            temporal.prepare()
    finally:
        capture.release()
        run.record["automatic_court_preparation"] = {
            "sampled_frames": sampled,
            "references": len(mapping.references.items),
            "observations": [
                {"time": r["time"], **r["evidence"]} for r in mapping.references.items
            ],
            "runtime_seconds": round(time.monotonic() - started, 3),
            "max_samples": MAX_SAMPLES,
            "max_seconds": MAX_SECONDS,
        }
        run.publish()


def sample(
    run: ClipRun, capture: object, model: object, timestamp: float
) -> tuple | None:
    """Keep original detector resolution and stop between decode and inference."""
    cv, _ = modules()
    decoder = cast("Any", capture)
    decoder.set(cv.CAP_PROP_POS_MSEC, timestamp * 1000)
    ok, image = decoder.read()
    if not ok or run.stopped():
        return None
    actual = decoder.get(cv.CAP_PROP_POS_MSEC) / 1000
    if abs(actual - timestamp) > REFERENCE_TIME_TOLERANCE:
        return None
    result = cast("Any", model).predict(
        image,
        device="cpu",
        imgsz=run.options.imgsz,
        conf=0.1,
        max_det=80,
        verbose=False,
    )[0]
    importlib.import_module("torch").set_num_threads(CPU_THREADS)
    if run.stopped():
        return None
    objects = static_objects(
        result, 0.1, labels=("ball", "basket", "player", "referee")
    )
    return actual, image, objects
