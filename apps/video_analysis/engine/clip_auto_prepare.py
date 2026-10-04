"""Bounded automatic reference preparation inside the clip's original deadline."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import importlib
import itertools
import time
from typing import TYPE_CHECKING, Any, cast
import warnings

from . import clip_basket_reference, clip_penalty_area
from .clip_auto_court import estimate
from .clip_inference import CPU_THREADS
from .clip_referee import (
    MIN_PLAYER_CONFIDENCE,
    MIN_REFEREE_CONFIDENCE,
    SAME_BODY_IOU,
    alike,
    kit,
    outvoted,
    overlap,
)
from .clip_signals import modules
from .clips import static_objects


if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray

    from .clip_auto_court import AutoCourt
    from .clips import ClipRun

MAX_SAMPLES = 96
MAX_SECONDS = 120
# A sideline camera that the first samples cannot place gets more of them:
# each round halves their spacing, within its own time budget.
FIELD_ROUNDS = 2
FIELD_SECONDS = 240
FIELD_RETRY = {
    "no_views",
    "one_post",
    "unsupported_camera",
    "ambiguous_camera",
    "unstable_camera",
}
SAMPLE_INTERVAL = 0.8
CONTEXT_SECONDS = 12
WINDOW_SAMPLES = 21
MIN_IMAGES = 5
MIN_PIXEL_OBSERVATIONS = 2
REFERENCE_TIME_TOLERANCE = 0.1
PROGRESS_SECONDS = 5
SAMPLE_QUALITY = 92
BRIDGE_INTERVAL = 0.15
MAX_BRIDGE_FRAMES = 12
MAX_BRIDGED_GAPS = 12
PALETTE_CONFIDENCE = 0.5


class TemporalReference:
    """Align visible observations and remove moving people before fitting markings."""

    def __init__(
        self, mapping: AutoCourt, stopped: Callable[[], bool] | None = None
    ) -> None:
        """Bound memory to one short camera view."""
        self.mapping = mapping
        self.stopped = stopped or (lambda: False)
        self.samples: deque = deque(maxlen=WINDOW_SAMPLES)

    def consider(self, image: NDArray[Any], timestamp: float, objects: list) -> None:
        """Prefer measured references, then collect complementary masked pixels."""
        if self.mapping.references.find(image, timestamp, objects):
            self.prepare()
            self.samples.clear()
            return
        found = estimate(image, objects, self.mapping.court)
        if found:
            self.mapping.references.add(image, timestamp, objects, found)
            self.prepare()
            self.samples.clear()
        else:
            self.add(image, timestamp, objects)
            if len(self.samples) == WINDOW_SAMPLES:
                self.prepare()
                self.samples.clear()

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


def prepare(run: ClipRun, video: Path | str, model: object) -> None:
    """Find clear references on either side of playback frames without another job.

    Sampled views first calibrate the fixed-camera model. The slower landmark
    references then run only for samples the camera model did not calibrate,
    with their own time budget, so they no longer cut sampling short.
    """
    mapping = run.camera.automatic
    if mapping is None:
        return
    cv, _ = modules()
    capture = cv.VideoCapture(
        str(video), cv.CAP_FFMPEG, [cv.CAP_PROP_N_THREADS, CPU_THREADS]
    )
    started = time.monotonic()
    sampled = 0
    context_sampled = 0
    camera_model: dict = {"groups": []}
    windows: list[list[tuple]] = []
    landmarks = {"samples": 0, "runtime_seconds": 0.0}
    field: dict = {"status": "not_needed"}
    try:
        for context, timestamps in preparation_windows(run):
            windows.append([])
            for timestamp in timestamps:
                if run.stopped() or time.monotonic() - started > MAX_SECONDS:
                    break
                snapshot = sample(run, capture, model, timestamp)
                if snapshot is None:
                    continue
                sampled += 1
                context_sampled += int(context)
                actual, image, objects = snapshot
                mapping.hall.solver.add_keyframe(image, actual, objects)
                windows[-1].append((actual, encode(image), objects))
                run.record["message"] = (
                    f"Preparing court references ({sampled} frames checked)"
                )
                if time.monotonic() - run.last_publish >= PROGRESS_SECONDS:
                    run.publish()
        whole_clip_evidence(run, windows)
        bridge(run, capture, mapping)
        camera_model = calibrate_camera(mapping, run.stopped)
        landmarks = landmark_references(run, mapping, windows, started)
        field = field_camera(run, (capture, model), mapping, windows)
    finally:
        capture.release()
        run.record["automatic_court_preparation"] = {
            "sampled_frames": sampled,
            "context_frames": context_sampled,
            "references": len(mapping.references.items),
            "observations": [
                {"time": r["time"], **r["evidence"]} for r in mapping.references.items
            ],
            "runtime_seconds": round(time.monotonic() - started, 3),
            "max_samples": MAX_SAMPLES,
            "max_seconds": MAX_SECONDS,
            "camera_model": camera_model,
            "landmark_references": landmarks,
            "field_camera": field,
        }
        run.publish()


def field_camera(
    run: ClipRun, source: tuple[object, object], mapping: AutoCourt, windows: list
) -> dict:
    """Fit a sideline tripod camera when neither a hall nor markings were found.

    The camera's turn between the korfs is followed through every frame of the
    clip; that needs no detector, only the frames themselves. `source` holds
    the video decoder and the detector. A far korf is seen in few frames, so
    when the sampled frames do not place the camera, the frames between them
    are detected too.
    """
    capture = source[0]
    if mapping.hall.groups or mapping.references.items:
        return {"status": "not_needed"}
    samples = windows[0] if windows else []
    if not mapping.field.promising([objects for _, _, objects in samples]):
        return {"status": "no_evidence"}
    # Like the camera model, this runs after sampling and outside its deadline:
    # a slow sampling pass must not cost the clip its only map.
    began = time.monotonic()
    aspect = follow_camera(run, capture, mapping)
    if aspect is None:
        return {"status": "interrupted"}
    for timestamp, _, objects in samples:
        mapping.field.sample(timestamp, objects, aspect)
    summary = mapping.field.fit()
    times = sorted(timestamp for timestamp, _, _ in samples)
    extra = 0
    for _ in range(FIELD_ROUNDS):
        if summary["status"] not in FIELD_RETRY:
            break
        added = between(run, source, mapping, times, (aspect, began))
        extra += len(added)
        # A cancelled run or a spent budget keeps the answer it already has.
        if not added or run.stopped() or time.monotonic() - began > FIELD_SECONDS:
            break
        times = sorted(times + added)
        summary = mapping.field.fit()
    summary["extra_samples"] = extra
    summary["runtime_seconds"] = round(time.monotonic() - began, 3)
    return summary


def between(
    run: ClipRun,
    source: tuple[object, object],
    mapping: AutoCourt,
    times: list[float],
    frame: tuple[float, float],
) -> list[float]:
    """Detect the frames halfway between sampled ones; return their times.

    `frame` holds the image aspect and when the field camera's work began.
    """
    capture, model = source
    aspect, began = frame
    added = []
    for before, after in itertools.pairwise(times):
        if run.stopped() or time.monotonic() - began > FIELD_SECONDS:
            break
        snapshot = sample(run, capture, model, (before + after) / 2)
        if snapshot is not None:
            mapping.field.sample(snapshot[0], snapshot[2], aspect)
            added.append(snapshot[0])
    return added


def follow_camera(run: ClipRun, capture: object, mapping: AutoCourt) -> float | None:
    """Track the camera through the clip's frames; return the image aspect."""
    cv, _ = modules()
    decoder = cast("Any", capture)
    start, end = run.options.start, run.options.start + run.options.duration
    decoder.set(cv.CAP_PROP_POS_MSEC, start * 1000)
    next_sample, aspect = start, None
    while not run.stopped() and decoder.grab():
        timestamp = decoder.get(cv.CAP_PROP_POS_MSEC) / 1000
        if timestamp >= end:
            break
        if timestamp + 1e-6 < next_sample:
            continue
        ok, image = decoder.retrieve()
        if not ok:
            break
        aspect = image.shape[1] / image.shape[0]
        mapping.field.track(image, timestamp)
        next_sample = max(next_sample + 1 / run.options.fps, timestamp + 1e-6)
    return None if run.stopped() else aspect


def encode(image: NDArray[Any]) -> bytes:
    """Hold a sampled view compactly until the landmark pass needs it."""
    cv, _ = modules()
    return cv.imencode(".jpg", image, [cv.IMWRITE_JPEG_QUALITY, SAMPLE_QUALITY])[
        1
    ].tobytes()


def landmark_references(
    run: ClipRun, mapping: AutoCourt, windows: list, started: float
) -> dict:
    """Fit landmark references only where the camera model left views uncalibrated.

    This shares the preparation deadline: nothing new starts after it.
    """
    cv, np = modules()
    began = time.monotonic()
    covered = {
        round(mapping.hall.solver.keyframes[k]["time"], 3)
        for group in mapping.hall.groups
        for k in group["members"]
    }
    temporal = TemporalReference(
        mapping, lambda: run.stopped() or time.monotonic() - started > MAX_SECONDS
    )
    used = 0
    for window in windows:
        # Non-adjacent windows are separate observations, even when the wall
        # looks similar. Every saved reference still needs its own floor fit.
        temporal.samples.clear()
        for timestamp, picture, objects in window:
            if temporal.stopped():
                break
            if round(timestamp, 3) in covered:
                continue
            image = cv.imdecode(np.frombuffer(picture, np.uint8), cv.IMREAD_COLOR)
            temporal.consider(image, timestamp, objects)
            used += 1
        if not temporal.stopped():
            temporal.prepare()
    return {"samples": used, "runtime_seconds": round(time.monotonic() - began, 3)}


def bridge(run: ClipRun, capture: object, mapping: AutoCourt) -> None:
    """Decode extra views where adjacent samples no longer overlap.

    A fast pan between the korfs can fall between samples. Intermediate frames
    need only whole-image features, so the detector is not run on them.
    """
    cv, _ = modules()
    decoder = cast("Any", capture)
    solver = mapping.hall.solver
    for start, end in solver.gaps()[:MAX_BRIDGED_GAPS]:
        steps = min(MAX_BRIDGE_FRAMES, max(1, int((end - start) / BRIDGE_INTERVAL)))
        for n in range(1, steps + 1):
            if run.stopped():
                return
            timestamp = start + (end - start) * n / (steps + 1)
            decoder.set(cv.CAP_PROP_POS_MSEC, timestamp * 1000)
            ok, image = decoder.read()
            if ok:
                solver.add_keyframe(image, decoder.get(cv.CAP_PROP_POS_MSEC) / 1000, [])


def calibrate_camera(mapping: AutoCourt, stopped: Callable[[], bool]) -> dict:
    """Solve every sampled view with every court observation in one pass.

    This runs after sampling, outside the sampling deadline: it is what lets one
    observation calibrate the clip's other views, both before and after it.
    """
    started = time.monotonic()
    try:
        summary = mapping.hall.calibrate(stopped)
    except (ValueError, ArithmeticError) as error:
        summary = {"groups": [], "error": type(error).__name__}
    summary["runtime_seconds"] = round(time.monotonic() - started, 3)
    return summary


def preparation_windows(run: ClipRun) -> list[tuple[bool, list[float]]]:
    """Spend remaining sample budget on nearby clear views of the same recording.

    Analyze the requested interval first. Context supplies calibration only and
    never extends the analyzed clip, joins camera cuts, or changes annotations.
    """
    start, duration = run.options.start, run.options.duration
    end = start + duration
    interval = max(SAMPLE_INTERVAL, duration / MAX_SAMPLES)
    primary = [
        start + i * interval for i in range(MAX_SAMPLES) if i * interval < duration
    ]
    windows = [(False, primary)]
    remaining = MAX_SAMPLES - len(primary)
    recording_end = run.match.get("duration_seconds", end)
    ranges = [
        (max(0, start - CONTEXT_SECONDS), start),
        (end, min(recording_end, end + CONTEXT_SECONDS)),
    ]
    available = sum(max(0, b - a) for a, b in ranges)
    if remaining and available:
        interval = max(SAMPLE_INTERVAL, available / remaining)
        for a, b in ranges:
            times = [a + i * interval for i in range(remaining) if a + i * interval < b]
            remaining -= len(times)
            if times:
                windows.append((True, times))
    return windows


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


def pixel_row(obj: dict, width: int, height: int) -> NDArray[Any]:
    """Return a sampled detection as a pixel corner box with its confidence."""
    _, np = modules()
    x, y, w, h = obj["bbox"]
    return np.array([
        x * width,
        y * height,
        (x + w) * width,
        (y + h) * height,
        obj["confidence"],
    ])


def whole_clip_evidence(run: ClipRun, windows: list) -> None:
    """Learn shirt colours and the referee's kit from the sampled views.

    Analysis runs forward, but the samples span the whole clip: what is only
    recognisable later (a referee the detector first calls a player, a team
    that is out of view at the start) is known from the first frame on.
    """
    cv, np = modules()
    kits, frames = [], []
    learn = run.teams.centers is None
    memory = run.people.referee
    for _, picture, objects in windows[0] if windows else []:
        image = cv.imdecode(np.frombuffer(picture, np.uint8), cv.IMREAD_COLOR)
        height, width = image.shape[:2]
        referees = [
            pixel_row(obj, width, height)
            for obj in objects
            if obj["label"] == "referee" and obj["confidence"] >= MIN_REFEREE_CONFIDENCE
        ]
        # Every player box may outvote a referee label, as in analysis; only
        # the surer ones are samples of what players wear.
        players = [
            (box := pixel_row(obj, width, height), kit(image, box), obj)
            for obj in objects
            if obj["label"] == "player" and obj["confidence"] >= MIN_PLAYER_CONFIDENCE
        ]
        worn = [(box, colour) for box, colour, _ in players]
        believed = [
            (box, colour)
            for box in referees
            if not outvoted(box, colour := kit(image, box), worn)
        ]
        # Analysis learns the kit from each frame's strongest referee box.
        if believed:
            kits.append(max(believed, key=lambda found: found[0][4])[1])
        samples = []
        for box, colour, obj in players:
            # A player box on the referee's body in their kit is the referee.
            if obj["confidence"] < PALETTE_CONFIDENCE or any(
                overlap(box, other) >= SAME_BODY_IOU and alike(colour, theirs)
                for other, theirs in believed
            ):
                continue
            shirt = run.teams.observe(image, obj["bbox"]) if learn else None
            samples.append((box, colour, shirt))
        frames.append(samples)
    memory.preload(kits, [])
    shirts, others = [], []
    for samples in frames:
        # The only body in the referee's kit may be the referee the detector
        # called a player: no evidence that players wear it, and no team.
        near = [box for box, colour, _ in samples if memory.close(colour)]
        alone = bool(near) and all(
            overlap(box, near[0]) >= SAME_BODY_IOU for box in near[1:]
        )
        for _, colour, shirt in samples:
            if alone and memory.close(colour):
                continue
            others.append(colour)
            if shirt is not None:
                shirts.append(shirt)
    learned = run.teams.learn_palette(shirts) if learn else False
    run.record["team_palette"] = {
        "status": "learned" if learned else "unclear" if learn else "supplied",
        "samples": len(shirts),
    }
    memory.preload([], others)
