"""Eleven-frame draft labeling with conservative, track-supported estimates."""

from __future__ import annotations

from fractions import Fraction
import hashlib
import importlib
from itertools import pairwise
import json
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING, Any, cast

from . import temporal_fusion
from .media import binary
from .store import Store, atomic_json
from .vision import digest


if TYPE_CHECKING:
    from .training import Detector, RunOptions

VERSION = 2
CONTEXT_VERSION = 1
WINDOW_SIZE = 11
MAX_CENTER_ERROR = 12
MAX_CUT_ERROR = 35
RADIUS = 5


def context(store: Store, match: dict, frame: dict) -> list[Path]:
    """Load a frozen window or extract five native frames on each side.

    Missing footage and clip boundaries return no context, never duplicate frames.
    """
    frozen = store.root / "temporal.json"
    if frozen.exists():
        paths = json.loads(frozen.read_text()).get(match["id"], {}).get(frame["id"], [])
        return [store.media(path) for path in paths]
    if not match.get("video") or not match.get("fps"):
        return []
    fps = float(Fraction(str(match["fps"])))
    time = float(frame["time_seconds"])
    if (
        fps <= 0
        or time < RADIUS / fps
        or time + RADIUS / fps >= match.get("duration_seconds", 0)
    ):
        return []
    video = store.media(match["video"])
    key = hashlib.sha256(
        json.dumps([
            match.get("video_sha256"),
            frame["image"],
            time,
            fps,
            CONTEXT_VERSION,
        ]).encode()
    ).hexdigest()
    directory = store.root / "vision/context" / key
    marker = directory / "complete.json"
    if marker.exists():
        return [directory / name for name in json.loads(marker.read_text())]
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            binary("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-ss",
            str(time - RADIUS / fps),
            "-i",
            str(video),
            "-frames:v",
            "11",
            "-vsync",
            "0",
            "-q:v",
            "2",
            str(directory / "%02d.jpg"),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    paths = [directory / f"{index:02d}.jpg" for index in range(1, 12)]
    if not all(path.is_file() for path in paths):
        return []
    atomic_json(marker, [path.name for path in paths])
    return paths


def freeze(store: Store, review: dict) -> tuple[bytes, dict[str, Path]]:
    """Include only selected windows in a checksummed remote kit, not full videos."""
    mapping: dict[str, Any] = {}
    files = {}
    for match in review["matches"]:
        for frame in match["frames"]:
            paths = context(store, match, frame)
            names = [path.relative_to(store.root).as_posix() for path in paths]
            mapping.setdefault(match["id"], {})[frame["id"]] = names
            files.update({
                "data/" + name: path for name, path in zip(names, paths, strict=True)
            })
    return json.dumps(mapping).encode(), files


def fuse(
    target: dict, tracks: list[dict[int, dict]], confidence: float
) -> tuple[dict, int]:
    """Associate and refine one target box per supported person track."""
    return temporal_fusion.fuse(target, tracks, confidence, RADIUS)


def predict(
    model: Detector,
    target: Path,
    paths: list[Path],
    options: RunOptions,
    baseline: dict,
) -> tuple[dict, dict]:
    """Track within one window and reject misaligned centers or camera cuts."""
    info = {"mode": "single-frame", "context_frames": 0, "estimated_objects": 0}
    if len(paths) != WINDOW_SIZE:
        return baseline, dict(
            info, reason="Full 5-before / 5-after context unavailable"
        )
    cv2 = importlib.import_module("cv2")
    np = importlib.import_module("numpy")
    images = [cv2.imread(str(path)) for path in paths]
    center = cv2.imread(str(target))
    if center is None or any(
        image is None or image.shape != center.shape for image in images
    ):
        return baseline, dict(info, reason="Context dimensions or decoding mismatch")
    small = [cv2.resize(image, (64, 36)).astype("float32") for image in images]
    if np.mean(np.abs(small[RADIUS] - cv2.resize(center, (64, 36)))) > MAX_CENTER_ERROR:
        return baseline, dict(info, reason="Context center does not match review image")
    if any(np.mean(np.abs(a - b)) > MAX_CUT_ERROR for a, b in pairwise(small)):
        return baseline, dict(info, reason="Camera cut or abrupt motion in context")
    images[RADIUS] = center
    predictor = getattr(model, "predictor", None)
    for tracker in getattr(predictor, "trackers", []):
        tracker.reset()
    tracks = []
    for image in images:
        result = model.track(
            image,
            persist=True,
            tracker=getattr(options, "tracker", "botsort") + ".yaml",
            device=options.device,
            imgsz=options.imgsz,
            conf=min(options.confidence, 0.1),
            verbose=False,
            max_det=80,
        )[0]
        tracks.append(track_objects(result, model.names))
    mode = getattr(options, "temporal_mode", "refine")
    combined, estimates = temporal_fusion.fuse(
        baseline, tracks, options.confidence, RADIUS, refine_existing=mode == "refine"
    )
    return combined, {
        "mode": "temporal",
        "context_frames": len(paths) - 1,
        "estimated_objects": estimates,
        "added_objects": len(combined["objects"]) - len(baseline["objects"]),
        "refined_objects": sum(
            original["bbox"] != refined["bbox"]
            for original, refined in zip(
                baseline["objects"], combined["objects"], strict=False
            )
        ),
        "context_sha256": [digest(path) for path in paths],
        "version": VERSION,
        "tracker": getattr(options, "tracker", "botsort"),
        "fusion_mode": mode,
    }


def track_objects(raw: object, names: dict[int, str]) -> dict[int, dict]:
    """Translate tracked people, preserving native detection-to-ID alignment."""
    result = cast(Any, raw)
    ids = (
        result.boxes.id.cpu().tolist()
        if result.boxes is not None and result.boxes.id is not None
        else []
    )
    # Translate preserves supported boxes only; match by native box index instead.
    step = {}
    if ids:
        for identity, box, cls, score in zip(
            ids,
            result.boxes.xyxyn.cpu().tolist(),
            result.boxes.cls.cpu().tolist(),
            result.boxes.conf.cpu().tolist(),
            strict=True,
        ):
            label = names[int(cls)]
            label = "player" if label == "person" else label
            if label not in {"player", "referee"}:
                continue
            x1, y1, x2, y2 = [min(1.0, max(0.0, float(value))) for value in box]
            if x2 > x1 and y2 > y1:
                step[int(identity)] = {
                    "label": label,
                    "bbox": [x1, y1, x2 - x1, y2 - y1],
                    "confidence": float(score),
                }
    return step


def manifest_digest(store: Store) -> str | None:
    """Bind inference reuse to the frozen temporal input manifest."""
    path = store.root / "temporal.json"
    return digest(path) if path.is_file() else None
