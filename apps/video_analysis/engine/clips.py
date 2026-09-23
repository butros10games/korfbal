"""Stream a bounded clip into private, inspectable chunks and a terminal receipt."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
import importlib
import json
import math
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, cast

from .clip_contract import CHUNK_FRAMES, MAX_RUNTIME_SECONDS, ClipOptions
from .clip_inference import CPU_THREADS, clip_detector
from .clip_models import MODEL_ERROR, failure_message, supports_clips
from .clip_positions import attach_post_distances
from .clip_references import suggestion
from .clip_replay import top_down
from .clip_signals import Camera, Teams, modules
from .clip_tracking import Balls, People
from .detect import ProjectionStore
from .store import Store, atomic_json
from .training import environment, weights_record
from .vision import digest, identifier


if TYPE_CHECKING:
    from numpy.typing import NDArray


PROGRESS_SECONDS = 5
REFERENCE_TIME_TOLERANCE = 0.1


def directory(store: Store, run_id: str) -> Path:
    """Resolve a clip artifact inside its owner's workspace."""
    return store.root / "vision" / "clips" / identifier(run_id)


def receipt(store: Store, run_id: str) -> dict:
    """Read the latest atomically published progress or terminal result."""
    return json.loads((directory(store, run_id) / "run.json").read_text())


class ClipRun:
    """Bound memory, preserve partial results, and make every attempt inspectable."""

    def __init__(self, root: Path, match: dict, options: ClipOptions) -> None:
        """Bind immutable inputs and establish a single wall-clock deadline."""
        self.root, self.match, self.options = root, match, options
        self.started = time.monotonic()
        self.last_publish = self.started
        self.buffer: list[dict] = []
        self.timings = {"decode": 0.0, "inference": 0.0, "tracking": 0.0}
        self.record: dict[str, Any] = {
            "id": root.name,
            "schema_version": 1,
            "kind": "clip",
            "status": "running",
            "created_at": datetime.now(UTC).isoformat(),
            "recipe": {"match_id": match["id"], "options": asdict(options)},
            "recording_title": match.get("title", match["id"]),
            "video": match["video"],
            "source_offset_seconds": match.get("source_offset_seconds", 0),
            "frames": 0,
            "chunks": [],
            "camera_cuts": 0,
            "active_ball_frames": 0,
            "court_frames": 0,
            "team_assigned": 0,
            "player_observations": 0,
            "review_only": True,
            "message": "Loading recording and model",
        }
        self.camera = Camera(options.court)
        self.people = People(options)
        self.balls = Balls()
        self.teams = Teams(options.team_colors)

    def publish(self) -> None:
        """Commit immutable frame chunks before publishing their manifest entries."""
        if self.buffer:
            name = f"chunk-{len(self.record['chunks']):05d}.json"
            atomic_json(self.root / name, {"frames": self.buffer})
            self.record["chunks"].append({
                "name": name,
                "start": self.buffer[0]["time_seconds"],
                "end": self.buffer[-1]["time_seconds"],
                "frames": len(self.buffer),
                "sha256": digest(self.root / name),
            })
            self.buffer.clear()
        self.record["runtime_seconds"] = round(time.monotonic() - self.started, 2)
        self.record["timings_seconds"] = {
            key: round(value, 3) for key, value in self.timings.items()
        }
        self.record["processed_fps"] = round(
            self.record["frames"] / max(0.001, time.monotonic() - self.started), 2
        )
        atomic_json(self.root / "run.json", self.record)
        self.last_publish = time.monotonic()

    def stopped(self) -> bool:
        """Check cancellation and the same deadline during decoding and inference."""
        if (self.root / "cancel.json").exists():
            self.record.update(
                status="cancelled", message="Stopped; partial results retained"
            )
        elif time.monotonic() - self.started > MAX_RUNTIME_SECONDS:
            self.record.update(
                status="interrupted",
                message="Runtime limit reached; partial results retained",
            )
        return self.record["status"] != "running"

    def finish(self) -> None:
        """Publish a terminal receipt even when the last chunk cannot be encoded."""
        self.record["finished_at"] = datetime.now(UTC).isoformat()
        try:
            self.publish()
        except Exception:
            self.record.update(
                status="failed",
                message="Could not publish final frames; earlier chunks retained",
                unpublished_frames=len(self.buffer),
            )
            atomic_json(self.root / "run.json", self.record)
            raise

    def frames(self, video: Path) -> Iterator[tuple[float, NDArray[Any]]]:
        """Decode sequentially, recording actual timestamps rather than guessed times.

        Yields:
            An actual source timestamp and one sampled BGR image.

        Raises:
            ValueError: The recording cannot supply the requested interval or cadence.

        """
        cv, _ = modules()
        capture = cv.VideoCapture(
            str(video), cv.CAP_FFMPEG, [cv.CAP_PROP_N_THREADS, CPU_THREADS]
        )
        try:
            fps = capture.get(cv.CAP_PROP_FPS)
            if (
                not capture.isOpened()
                or not math.isfinite(fps)
                or fps < self.options.fps
            ):
                raise ValueError("Recording cannot supply the requested frame rate")
            capture.set(cv.CAP_PROP_POS_MSEC, self.options.start * 1000)
            next_sample, last_timestamp = self.options.start, -1.0
            end = self.options.start + self.options.duration
            while next_sample < end - 1e-6 and not self.stopped():
                started = time.monotonic()
                ok = capture.grab()
                self.timings["decode"] += time.monotonic() - started
                if not ok:
                    if next_sample < end - 1 / fps - 1e-6:
                        raise ValueError(
                            "Recording ended before the requested interval"
                        )
                    break
                timestamp = capture.get(cv.CAP_PROP_POS_MSEC) / 1000
                if not math.isfinite(timestamp) or timestamp <= last_timestamp:
                    raise ValueError("Decoder timestamps must increase")
                last_timestamp = timestamp
                if timestamp + 1e-6 < next_sample:
                    continue
                if timestamp >= end:
                    break
                started = time.monotonic()
                ok, image = capture.retrieve()
                self.timings["decode"] += time.monotonic() - started
                if not ok:
                    raise ValueError("Could not retrieve the sampled frame")
                yield timestamp, image
                # Avoid duplicating frames when decoding a variable-rate source.
                next_sample = max(next_sample + 1 / self.options.fps, timestamp + 1e-6)
        finally:
            capture.release()

    def step(self, image: NDArray[Any], timestamp: float, result: object) -> None:
        """Combine identities with independent colour, ball and floor signals."""
        raw = cast("Any", result)
        boxes = [
            [x1, y1, x2 - x1, y2 - y1] for x1, y1, x2, y2 in raw.boxes.xyxyn.tolist()
        ]
        observations = static_objects(
            raw, 0.1, labels=("ball", "basket", "player", "referee")
        )
        camera = self.camera.update(image, timestamp, boxes, observations)
        camera["calibration"]["suggested_points"] = suggestion(
            camera["floor"], self.options.court, camera["calibration"], boxes
        )
        if camera["cut"]:
            self.people.reset(camera["segment"])
            self.balls.reset(camera["segment"])
            self.teams.reset()
            self.record["camera_cuts"] += 1
        persons = self.people.update(result, image, timestamp, camera)
        self.teams.update(image, persons, timestamp)
        detected = static_objects(result, self.options.confidence)
        balls, active = self.balls.update(
            [o for o in detected if o["label"] == "ball"],
            persons,
            timestamp,
            camera["motion"],
        )
        attach_post_distances(persons, self.options.court)
        objects = persons + balls + [o for o in detected if o["label"] == "basket"]
        self.buffer.append({
            "time_seconds": round(timestamp, 6),
            "segment": camera["segment"],
            "camera_cut": camera["cut"],
            "court_available": camera["floor"] is not None,
            "calibration": camera["calibration"],
            "active_ball": active,
            "objects": objects,
            "top_down": top_down(
                objects, active, self.options.court, camera["calibration"]
            ),
        })
        self.record["frames"] += 1
        self.record["active_ball_frames"] += int(active["status"] == "observed")
        self.record["court_frames"] += int(camera["floor"] is not None)
        self.record["team_assigned"] += sum(
            o.get("team") in {"team_a", "team_b"} for o in persons
        )
        self.record["player_observations"] += sum(
            o["label"] == "player" for o in persons
        )
        self.record["team_colors"] = self.teams.colors()
        if (
            len(self.buffer) >= CHUNK_FRAMES
            or time.monotonic() - self.last_publish >= PROGRESS_SECONDS
        ):
            self.publish()

    def execute(self, store: Store, weights: str) -> None:
        """Load one model, stream inference, and always publish a terminal receipt.

        Raises:
            ValueError: The model is incompatible or no frames were decoded.

        """
        atomic_json(self.root / "run.json", self.record)
        try:
            if self.stopped():
                return
            cv, _ = modules()
            cv.setNumThreads(CPU_THREADS)
            torch = importlib.import_module("torch")
            torch.set_num_threads(CPU_THREADS)
            video = store.media(self.match["video"])
            self.record["video_sha256"] = digest(video)
            model = cast(
                "Any", clip_detector(weights, store.root / "vision" / "cpu-cache")
            )
            self.record.update(weights_record(weights, model))
            expected = self.record["recipe"].get("weights_sha256")
            if expected and self.record["weights_sha256"] != expected:
                raise ValueError("Checkpoint changed while the clip was starting")
            self.record["environment"] = environment()
            if not supports_clips(list(model.names.values())):
                self.record["failure_code"] = "incompatible_model"
                raise ValueError(MODEL_ERROR)
            self.prepare_court(video, model)
            if self.stopped():
                return
            self.record["message"] = "Analyzing clip"
            self.publish()
            for timestamp, image in self.frames(video):
                started = time.monotonic()
                result = model.predict(
                    image,
                    device="cpu",
                    imgsz=self.options.imgsz,
                    conf=0.1,
                    max_det=80,
                    verbose=False,
                )[0]
                self.timings["inference"] += time.monotonic() - started
                self.record["inference"] = getattr(
                    model, "inference_info", {"backend": "torch", "precision": "fp32"}
                )
                # Ultralytics CPU setup can reset the pool after device changes.
                torch.set_num_threads(CPU_THREADS)
                self.record["cpu_threads"] = torch.get_num_threads()
                if self.stopped():
                    break
                started = time.monotonic()
                self.step(image, timestamp, result)
                self.timings["tracking"] += time.monotonic() - started
            if self.record["status"] == "running":
                if not self.record["frames"]:
                    raise ValueError("No frames were decoded")
                self.record.update(
                    status="completed", message="Clip ready for inspection"
                )
        except Exception as error:
            self.record.update(
                status="failed",
                message=failure_message(
                    self.record,
                    f"Clip failed ({type(error).__name__}); partial results kept",
                ),
            )
            raise
        finally:
            self.finish()

    def prepare_court(self, video: Path, model: object) -> None:
        """Decode exact reference frames within the same inference deadline.

        Raises:
            ValueError: A reference frame cannot be decoded accurately.

        """
        if not self.camera.mapping:
            return
        cv, _ = modules()
        torch = importlib.import_module("torch")
        capture = cv.VideoCapture(
            str(video), cv.CAP_FFMPEG, [cv.CAP_PROP_N_THREADS, CPU_THREADS]
        )
        prepared = []
        try:
            for anchor in self.camera.mapping.court["anchors"]:
                if self.stopped():
                    return
                capture.set(cv.CAP_PROP_POS_MSEC, anchor["time"] * 1000)
                ok, image = capture.read()
                if (
                    not ok
                    or abs(capture.get(cv.CAP_PROP_POS_MSEC) / 1000 - anchor["time"])
                    > REFERENCE_TIME_TOLERANCE
                ):
                    raise ValueError("Could not decode the requested reference frame")
                result = cast("Any", model).predict(
                    image,
                    device="cpu",
                    imgsz=self.options.imgsz,
                    conf=0.1,
                    max_det=80,
                    verbose=False,
                )[0]
                torch.set_num_threads(CPU_THREADS)
                boxes = [
                    [a, b, c - a, d - b] for a, b, c, d in result.boxes.xyxyn.tolist()
                ]
                prepared.append(self.camera.mapping.add_reference(image, anchor, boxes))
                self.record["court_references"] = prepared
                self.record["message"] = (
                    f"Prepared {len(prepared)} court reference frames"
                )
                self.publish()
        finally:
            capture.release()


def static_objects(
    raw: object, confidence: float, *, labels: tuple[str, ...] = ("ball", "basket")
) -> list[dict]:
    """Preserve basket and ball observations independently of people association."""
    result = cast("Any", raw)
    detected = []
    boxes = result.boxes.cpu().numpy()
    for box, cls, score in zip(
        boxes.xyxyn.tolist(),
        boxes.cls.tolist(),
        boxes.conf.tolist(),
        strict=True,
    ):
        label = result.names[int(cls)]
        if label not in labels or score < confidence:
            continue
        x1, y1, x2, y2 = [max(0.0, min(1.0, float(v))) for v in box]
        if x2 > x1 and y2 > y1:
            detected.append({
                "label": label,
                "bbox": [x1, y1, x2 - x1, y2 - y1],
                "confidence": float(score),
            })
    return detected


def analyze(
    store: Store, run_id: str, match: dict, weights: str, options: ClipOptions
) -> dict:
    """Run once; never save human labels or silently repeat an interrupted attempt.

    Raises:
        ValueError: The identifier already belongs to another or interrupted run.

    """
    options.for_recording(match)
    root = directory(store, run_id)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "run.json"
    recipe = {
        "match_id": match["id"],
        "options": asdict(options),
        "weights_sha256": digest(Path(weights)),
    }
    if marker.exists():
        previous = json.loads(marker.read_text())
        if previous.get("recipe") != recipe:
            raise ValueError("Clip ID belongs to a different request")
        if previous["status"] == "completed":
            return previous
        raise ValueError("This attempt already started; create a new run to retry")
    run = ClipRun(root, match, options)
    run.record["recipe"] = recipe
    run.execute(store, weights)
    return run.record


def main() -> None:
    """Run a fresh database projection in the isolated detector environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text())
    analyze(
        ProjectionStore(args.root, args.input),
        payload["run_id"],
        payload["match"],
        payload["weights"],
        ClipOptions.parse(payload["options"]),
    )


if __name__ == "__main__":
    main()
