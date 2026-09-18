"""Prepare local recordings and a clearly synthetic review fixture."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from operator import itemgetter
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from .store import SAFE_ID, Store, blank_annotation, number


@dataclass(frozen=True)
class ImportOptions:
    """Bounded frame extraction on the local recording's timeline."""

    match_id: str
    title: str
    source_url: str = ""
    source_offset: float = 0
    start: float = 0
    interval: float = 2
    count: int = 24
    split_group: str = ""


def binary(name: str) -> str:
    """Find a required media tool using the current PATH.

    Raises:
        ValueError: If input data or the requested operation is invalid.

    """
    result = shutil.which(name)
    if not result:
        raise ValueError(f"{name} is required to prepare recordings; install it first.")
    return result


def probe(path: Path) -> dict[str, Any]:
    """Read duration, frame rate, and actual dimensions from a local recording."""
    result = subprocess.run(
        [
            binary("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    raw = json.loads(result.stdout)
    stream = raw["streams"][0]
    return {
        "duration_seconds": float(raw["format"]["duration"]),
        "width": stream["width"],
        "height": stream["height"],
        "fps": stream["avg_frame_rate"],
    }


def import_recording(
    store: Store, source: Path, options: ImportOptions
) -> dict[str, Any]:
    """Copy a recording and extract reproducible full-size frames.

    Raises:
        OSError: If a local recording cannot be read or written.
        SubprocessError: If media extraction fails.
        ValueError: If input data or the requested operation is invalid.

    """
    if not SAFE_ID.fullmatch(options.match_id):
        raise ValueError(
            "Match ID must contain only letters, numbers, underscores, and hyphens"
        )
    if not source.is_file() or source.suffix.lower() not in {".mp4", ".webm"}:
        raise ValueError("Choose an existing local MP4 or WebM recording")
    number(options.interval, 0.04, 3600)
    number(options.source_offset, 0, 86400)
    number(options.count, 1, 1000)
    metadata = probe(source)
    number(options.start, 0, metadata["duration_seconds"])
    directory = store.root / options.match_id
    if directory.exists():
        raise ValueError("Match directory already exists; use a different ID")
    directory.mkdir()
    try:
        video = directory / f"recording{source.suffix.lower()}"
        shutil.copyfile(source, video)
        with video.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        frames = _extract_frames(store, video, metadata, options)
        if not frames:
            raise ValueError("No frames available in this interval")
        match = {
            "id": options.match_id,
            "title": options.title,
            "source_url": options.source_url,
            "source_offset_seconds": options.source_offset,
            "match_start_seconds": None,
            "split_group": options.split_group or options.match_id,
            "video": str(video.relative_to(store.root)),
            "video_sha256": checksum,
            "synthetic": False,
            **metadata,
            "frames": frames,
        }
        store.add_match(match)
    except (ValueError, OSError, subprocess.SubprocessError):
        shutil.rmtree(directory)
        raise
    return match


def _extract_frames(
    store: Store, video: Path, metadata: dict[str, Any], options: ImportOptions
) -> list[dict[str, Any]]:
    frames = []
    for index in range(options.count):
        time = options.start + index * options.interval
        if time >= metadata["duration_seconds"]:
            break
        image = video.parent / f"frame-{index:05d}.jpg"
        subprocess.run(
            [
                binary("ffmpeg"),
                "-v",
                "error",
                "-ss",
                str(time),
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(image),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        if not image.exists():
            raise ValueError(f"Could not decode frame at {time} seconds")
        frames.append({
            "id": f"frame-{index:05d}",
            "time_seconds": time,
            "source_time_seconds": options.source_offset + time,
            "image": str(image.relative_to(store.root)),
            "status": "pending",
            "proposal": None,
            "correction": None,
            "complete": False,
        })
    return frames


def create_demo(store: Store) -> None:
    """Seed drawn, synthetic footage for tests and public visual evidence only."""
    directory = store.root / "demo"
    directory.mkdir(exist_ok=True)
    svg = (Path(__file__).parent / "demo.svg").read_text()
    image = directory / "court.svg"
    image.write_text(svg)
    annotation = blank_annotation()
    annotation.update(
        scene="live",
        notes=(
            "Synthetic example. Check the ball box and add missing players "
            "before approval."
        ),
        objects=[
            {"label": "ball", "bbox": [0.595, 0.366, 0.04, 0.06], "confidence": 0.61},
            {
                "label": "basket",
                "bbox": [0.710, 0.446, 0.056, 0.065],
                "confidence": 0.92,
            },
            {"label": "player", "bbox": [0.39, 0.435, 0.075, 0.25], "confidence": 0.86},
        ],
    )
    frames = [
        {
            "id": f"frame-{i:05d}",
            "time_seconds": 754 + i * 2,
            "source_time_seconds": 754 + i * 2,
            "image": "demo/court.svg",
            "status": "pending",
            "proposal": annotation,
            "model": "synthetic fixture",
            "correction": None,
            "complete": False,
        }
        for i in range(8)
    ]
    store.add_match({
        "id": "demo",
        "title": "North vs South · practice match",
        "source_url": "",
        "source_offset_seconds": 0,
        "match_start_seconds": 120,
        "split_group": "demo",
        "video": None,
        "synthetic": True,
        "duration_seconds": 3600,
        "width": 1280,
        "height": 720,
        "fps": "25/1",
        "frames": frames,
    })


def sample_frame(store: Store, match_id: str, seconds: float) -> dict[str, Any]:
    """Extract a reviewer-selected playhead frame without replacing existing labels.

    Raises:
        ValueError: If the match, timestamp, or recording is unavailable.

    """
    data = store.read()
    match = next((m for m in data["matches"] if m["id"] == match_id), None)
    if match is None or not match.get("video"):
        raise ValueError("Choose a match with a recording")
    time = number(seconds, 0, match["duration_seconds"])
    frame_id = f"at-{round(time * 1000):09d}"
    if any(f["id"] == frame_id for f in match["frames"]):
        return {"frame_id": frame_id}
    video = store.media(match["video"])
    with tempfile.TemporaryDirectory(prefix="frame-", dir=video.parent) as temporary:
        image = Path(temporary) / "sample.jpg"
        subprocess.run(
            [
                binary("ffmpeg"),
                "-v",
                "error",
                "-ss",
                str(time),
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(image),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        if not image.exists():
            raise ValueError("No decodable frame at this time; seek slightly earlier")
        with store.transaction():
            data = store.read()
            match = next(m for m in data["matches"] if m["id"] == match_id)
            if any(f["id"] == frame_id for f in match["frames"]):
                return {"frame_id": frame_id}
            target = video.parent / f"{frame_id}.jpg"
            image.replace(target)
            match["frames"].append({
                "id": frame_id,
                "time_seconds": time,
                "source_time_seconds": match["source_offset_seconds"] + time,
                "image": str(target.relative_to(store.root)),
                "status": "pending",
                "proposal": None,
                "correction": None,
                "complete": False,
            })
            match["frames"].sort(key=itemgetter("time_seconds"))
            store._persist(data)
    return {"frame_id": frame_id}
