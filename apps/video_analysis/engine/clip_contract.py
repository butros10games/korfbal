"""Bounded, reproducible clip requests; no detector imports on the web process."""

from dataclasses import asdict, dataclass
from itertools import combinations
import math
from operator import itemgetter


CORNERS = 4
DIMENSIONS = 2
RGB_CHANNELS = 3
MIN_CORNER_TURN = 0.005
MAX_FRAMES = 6000
CHUNK_FRAMES = 100
MAX_RUNTIME_SECONDS = 3600
MAX_ANCHORS = 16
MAX_LANDMARKS = 16


def landmark_points(points: object) -> list[dict]:
    """Require distributed, distinct correspondences on the court floor.

    Raises:
        ValueError: The points cannot constrain a floor mapping.

    """
    if not isinstance(points, list) or not CORNERS <= len(points) <= MAX_LANDMARKS:
        raise ValueError("Mark 4 to 16 visible floor landmarks per reference frame")
    cleaned = []
    for point in points:
        if not isinstance(point, dict) or set(point) != {"image", "court"}:
            raise ValueError("Each landmark needs an image point and a court point")
        pair = {}
        for name in ("image", "court"):
            xy = point[name]
            if not isinstance(xy, list) or len(xy) != DIMENSIONS:
                raise ValueError("Invalid landmark coordinates")
            pair[name] = [finite(v, 0, 1) for v in xy]
        cleaned.append(pair)
    for name in ("image", "court"):
        positions = [p[name] for p in cleaned]
        if len({tuple(p) for p in positions}) != len(positions):
            raise ValueError("Use different landmarks")
        area = max(
            abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
            for a in positions
            for b in positions
            for c in positions
        )
        if area < MIN_CORNER_TURN:
            raise ValueError("Spread landmarks across the visible floor, not one line")
        if not any(
            all(
                abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
                > MIN_CORNER_TURN / 10
                for a, b, c in combinations(group, 3)
            )
            for group in combinations(positions, 4)
        ):
            raise ValueError("Choose four landmarks with no three on the same line")
    return cleaned


def finite(value: object, low: float, high: float) -> float:
    """Validate a finite JSON number without accepting booleans or strings.

    Raises:
        ValueError: The request or input does not satisfy this operation.

    """
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
    ):
        raise ValueError("Expected a finite number")
    if not low <= value <= high:
        raise ValueError(f"Choose a value between {low:g} and {high:g}")
    return float(value)


def reference_calibration(value: dict) -> dict:
    """Validate timestamped floor references.

    Raises:
        ValueError: References are missing, duplicated or malformed.

    """
    anchors = value["anchors"]
    if not isinstance(anchors, list) or not 1 <= len(anchors) <= MAX_ANCHORS:
        raise ValueError("Use 1 to 16 court reference frames")
    cleaned = []
    for anchor in anchors:
        if not isinstance(anchor, dict) or set(anchor) != {"time", "points"}:
            raise ValueError("Each reference frame needs a timestamp and landmarks")
        cleaned.append({
            "time": finite(anchor["time"], 0, 86400),
            "points": landmark_points(anchor["points"]),
        })
    if len({a["time"] for a in cleaned}) != len(cleaned):
        raise ValueError("Reference frame timestamps must be different")
    return {
        "anchors": sorted(cleaned, key=itemgetter("time")),
        "length": finite(value["length"], 5, 100),
        "width": finite(value["width"], 5, 100),
    }


def calibration(value: object) -> dict | None:
    """Bind four ordered image corners to a rectangular floor, never to the ball.

    Raises:
        ValueError: The request or input does not satisfy this operation.

    """
    if value is None:
        return None
    if isinstance(value, dict) and set(value) == {"anchors", "length", "width"}:
        return reference_calibration(value)
    if not isinstance(value, dict) or set(value) != {"corners", "length", "width"}:
        raise ValueError("Court calibration needs four corners, length and width")
    points = value["corners"]
    if not isinstance(points, list) or len(points) != CORNERS:
        raise ValueError("Select all four court corners in order")
    points = [
        [finite(p[0], 0, 1), finite(p[1], 0, 1)]
        if isinstance(p, list) and len(p) == DIMENSIONS
        else []
        for p in points
    ]
    if any(len(p) != DIMENSIONS for p in points):
        raise ValueError("Invalid court corner")
    turns = []
    for i in range(4):
        a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        turns.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    if not (min(turns) > MIN_CORNER_TURN or max(turns) < -MIN_CORNER_TURN):
        raise ValueError("Court corners must form a visible convex quadrilateral")
    return {
        "corners": points,
        "length": finite(value["length"], 5, 100),
        "width": finite(value["width"], 5, 100),
    }


@dataclass(frozen=True)
class ClipOptions:
    """Sampling and optional user calibration for one continuous clip."""

    start: float = 0
    duration: float = 20
    fps: float = 12.5
    imgsz: int = 1280
    confidence: float = 0.25
    tracker: str = "botsort"
    team_colors: list[list[float]] | None = None
    court: dict | None = None

    @classmethod
    def parse(cls, payload: dict) -> "ClipOptions":
        """Reject unbounded work and malformed calibration before scheduling.

        Raises:
            TypeError: The request or input does not satisfy this operation.
            ValueError: The request or input does not satisfy this operation.

        """
        if not isinstance(payload, dict):
            raise TypeError("Expected clip options")
        unknown = set(payload) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("Unknown clip options")
        defaults = asdict(cls())
        defaults.update(payload)
        for name, low, high in (
            ("start", 0, 86400),
            ("duration", 1, 600),
            ("fps", 5, 25),
            ("confidence", 0.1, 0.9),
            ("imgsz", 640, 1600),
        ):
            defaults[name] = finite(defaults[name], low, high)
        if defaults["duration"] * defaults["fps"] > MAX_FRAMES:
            raise ValueError(f"Limit a clip to {MAX_FRAMES} analyzed frames")
        if defaults["imgsz"] % 32:
            raise ValueError("Image size must be a multiple of 32")
        defaults["imgsz"] = int(defaults["imgsz"])
        if defaults["tracker"] not in {"botsort", "bytetrack"}:
            raise ValueError("Choose BoT-SORT or ByteTrack")
        colors = defaults["team_colors"]
        if colors is not None:
            if not isinstance(colors, list) or len(colors) != DIMENSIONS:
                raise ValueError("Choose two shirt colours")
            if any(not isinstance(c, list) or len(c) != RGB_CHANNELS for c in colors):
                raise ValueError("Colours must have three RGB components")
            defaults["team_colors"] = [[finite(v, 0, 255) for v in c] for c in colors]
        defaults["court"] = calibration(defaults["court"])
        return cls(**defaults)

    def for_recording(self, recording: dict) -> None:
        """Require real footage and a complete, in-bounds requested interval.

        Raises:
            ValueError: The request or input does not satisfy this operation.

        """
        if not recording.get("video"):
            raise ValueError("This recording has no available footage")
        end = finite(recording.get("duration_seconds"), 1, 86400)
        if self.start + self.duration > end + 0.001:
            raise ValueError("The clip extends past the end of the recording")
        if self.court and any(a["time"] >= end for a in self.court.get("anchors", [])):
            raise ValueError("Court references must be within the same recording")
