"""Person crops for roster-review questions, taken from immutable clip chunks.

Each question names one pure tracklet. Its observations come from the clip's
``fragment_identity`` frame aliases; boxes come from the published chunks, never
from a fresh detection. Crops are content-addressed JPEGs inside the clip run,
so repeated snapshots reuse them and nothing outside the run is written.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .clip_signals import modules


if TYPE_CHECKING:
    from numpy.typing import NDArray


DIRECTORY = "identity-crops"
QUESTIONS = 8
VIEWS = 5
HEIGHT = 240
PADDING = 0.12
TIME_TOLERANCE = 0.03
# Seek slightly early and decode forward; nearby later moments need no seek.
SEEK_LEAD = 0.1
FORWARD_SECONDS = 0.5
MAX_CHUNK_BYTES = 64 * 1024 * 1024
BOX_VALUES = 4
MIN_PIXELS = 2
# A few dozen seeks per snapshot; keep the worker's two-core budget.
DECODE_THREADS = 2


def crop_name(time_seconds: float, track_id: str) -> str:
    """Name a crop by its immutable source observation.

    Returns:
        A short file name that cannot leave the crop directory.

    """
    key = f"{time_seconds:.4f}|{track_id}".encode()
    return hashlib.sha256(key).hexdigest()[:24] + ".jpg"


def views(question: dict, observations: list[tuple[float, str]]) -> list[dict]:
    """Choose the asked moment plus evenly spread neighbours of the same tracklet.

    Returns:
        Up to ``VIEWS`` source observations, the asked moment first.

    """
    if not observations:
        track = question.get("source_track_id")
        return (
            [{"time_seconds": question["time_seconds"], "track_id": track}]
            if track
            else []
        )
    ordered = sorted(observations)
    asked = min(ordered, key=lambda o: abs(o[0] - question["time_seconds"]))
    picks = [asked]
    for index in range(VIEWS):
        candidate = ordered[round(index * (len(ordered) - 1) / max(1, VIEWS - 1))]
        if candidate not in picks:
            picks.append(candidate)
    return [{"time_seconds": t, "track_id": track} for t, track in picks[:VIEWS]]


def boxes(chunks: list[Path], wanted: set[tuple[float, str]]) -> dict:
    """Read only the needed boxes from chunks that cover the wanted moments.

    Returns:
        Normalized ``[x, y, w, h]`` boxes keyed by rounded time and track.

    Raises:
        ValueError: A chunk exceeds the bounded size.

    """
    found: dict[tuple[float, str], list[float]] = {}
    times = sorted({t for t, _ in wanted})
    for path in chunks:
        if not path.is_file() or not times:
            continue
        if path.stat().st_size > MAX_CHUNK_BYTES:
            raise ValueError("Clip chunk exceeds its bound")
        for frame in json.loads(path.read_text(encoding="utf-8")).get("frames", []):
            moment = round(float(frame["time_seconds"]), 3)
            for item in frame.get("objects", []):
                key = moment, str(item.get("track_id"))
                if key in wanted and len(item.get("bbox", [])) == BOX_VALUES:
                    found[key] = [float(v) for v in item["bbox"]]
    return found


def extract(
    run: Path,
    video: str,
    questions: list[dict],
    refinement: dict,
    chunks: list[Path],
) -> dict[str, list[dict]]:
    """Write missing crops for the next questions and describe every view.

    Returns:
        Per question identity, its views with crop name (or ``None``) and time.

    """
    observations: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for link in refinement.get("frame_links", []):
        if link.get("fragment_identity"):
            observations[link["fragment_identity"]].append((
                round(float(link["time_seconds"]), 3),
                str(link["from_track_id"]),
            ))
    plan = {
        q["identity"]: views(q, observations.get(q["identity"], []))
        for q in questions[:QUESTIONS]
    }
    wanted = {
        (round(v["time_seconds"], 3), str(v["track_id"]))
        for items in plan.values()
        for v in items
    }
    found = boxes(chunks, wanted)
    directory = run / DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    missing = sorted(
        (key, box)
        for key, box in found.items()
        if not (directory / crop_name(*key)).is_file()
    )
    if missing:
        write_crops(video, directory, missing)
    result: dict[str, list[dict]] = {}
    for identity, items in plan.items():
        result[identity] = []
        for view in items:
            name = crop_name(round(view["time_seconds"], 3), str(view["track_id"]))
            result[identity].append({
                **view,
                "crop": name if (directory / name).is_file() else None,
            })
    return result


def write_crops(
    video: str, directory: Path, missing: list[tuple[tuple[float, str], list[float]]]
) -> None:
    """Decode each needed moment once and save padded, bounded-height crops."""
    by_time: dict[float, list[tuple[str, list[float]]]] = defaultdict(list)
    for (moment, track), box in missing:
        by_time[moment].append((track, box))
    decoder = Decoder(video)
    try:
        for moment, items in sorted(by_time.items()):
            image = decoder.frame(moment)
            if image is None:
                continue
            for track, box in items:
                encoded = decoder.crop(image, box)
                if encoded is not None:
                    target = directory / crop_name(moment, track)
                    temporary = target.with_suffix(".tmp")
                    temporary.write_bytes(encoded)
                    temporary.replace(target)
    finally:
        decoder.capture.release()


class Decoder:
    """One sequentially seeking OpenCV reader for a private recording."""

    def __init__(self, video: str) -> None:
        """Open the recording with a bounded decoder thread count.

        Raises:
            ValueError: The recording cannot be opened.

        """
        self.cv, _ = modules()
        self.capture = self.cv.VideoCapture(
            str(video), self.cv.CAP_FFMPEG, [self.cv.CAP_PROP_N_THREADS, DECODE_THREADS]
        )
        if not self.capture.isOpened():
            raise ValueError("Recording cannot be opened for review crops")
        self.position = -1.0

    def frame(self, moment: float) -> NDArray[Any] | None:
        """Decode the frame the clip recorded at ``moment``.

        Clip times are the decoder's position after each grab, so the same
        convention selects the identical frame. Moments shortly ahead are
        reached by grabbing forward; others by a precise seek.

        Returns:
            The BGR image at ``moment``, or ``None`` when it is not decodable.

        """
        cv, capture = self.cv, self.capture
        if not self.position < moment <= self.position + FORWARD_SECONDS:
            capture.set(cv.CAP_PROP_POS_MSEC, max(0.0, moment - SEEK_LEAD) * 1000)
            self.position = -1.0
        while capture.grab():
            self.position = capture.get(cv.CAP_PROP_POS_MSEC) / 1000
            if self.position + 1e-6 < moment:
                continue
            if abs(self.position - moment) > TIME_TOLERANCE:
                return None
            ok, image = capture.retrieve()
            return image if ok else None
        return None

    def crop(self, image: NDArray[Any], box: list[float]) -> bytes | None:
        """Encode a padded person crop at the review height.

        Returns:
            JPEG bytes, or ``None`` for a degenerate box.

        """
        height, width = image.shape[:2]
        x, y, w, h = box
        left = max(0, math.floor((x - w * PADDING) * width))
        top = max(0, math.floor((y - h * PADDING) * height))
        right = min(width, math.ceil((x + w + w * PADDING) * width))
        bottom = min(height, math.ceil((y + h + h * PADDING) * height))
        if right - left < MIN_PIXELS or bottom - top < MIN_PIXELS:
            return None
        crop = image[top:bottom, left:right]
        scale = HEIGHT / crop.shape[0]
        crop = self.cv.resize(
            crop,
            (max(1, round(crop.shape[1] * scale)), HEIGHT),
            interpolation=self.cv.INTER_AREA if scale < 1 else self.cv.INTER_CUBIC,
        )
        ok, encoded = self.cv.imencode(".jpg", crop, [self.cv.IMWRITE_JPEG_QUALITY, 88])
        return encoded.tobytes() if ok else None
