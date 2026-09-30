"""Sync a recording stitched from camera files by the files' own start times.

A camera that splits a recording into files loses a few seconds at every
split. Each original file records when it started, so the stitched video can
be synced exactly: every join becomes a recording break with the seconds the
camera missed, and each tracked period's start is found in the file that was
recording at that moment. Joins with a long gap (half-time) need no break;
the next period's sync point covers them.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from apps.game_tracker.models import MatchPart


# Start times have whole-second precision, so an adjacent file can seem to
# start up to a second before the previous one ended.
CLOCK_PRECISION_SECONDS = 1.0
DURATION_TOLERANCE_SECONDS = 0.5


@dataclass(frozen=True)
class CameraFile:
    """One original camera file: when it started and how long it runs."""

    name: str
    started_at: datetime
    duration: float


@dataclass(frozen=True)
class CameraJoin:
    """Where two files meet in the stitched video and the real time between them."""

    video_seconds: float
    gap_seconds: float
    is_break: bool


@dataclass(frozen=True)
class CameraSync:
    """Breaks and period sync points derived from the camera files."""

    joins: list[CameraJoin]
    breaks: list[tuple[float, float]]
    anchors: dict[str, float]


def plan(
    files: Sequence[CameraFile],
    *,
    duration: float,
    parts: Sequence[MatchPart],
    max_gap: float,
) -> CameraSync:
    """Derive breaks and period starts for a recording stitched from ``files``.

    Returns:
        The joins, breaks and the sync point of every period the video shows.

    Raises:
        ValueError: The files overlap or do not add up to the recording.

    """
    ordered = sorted(files, key=lambda row: row.started_at)
    total = sum(row.duration for row in ordered)
    if abs(total - duration) > DURATION_TOLERANCE_SECONDS:
        raise ValueError(
            f"The files last {total:.3f} s but the recording lasts {duration:.3f} s."
        )
    joins: list[CameraJoin] = []
    starts: list[float] = []
    at = 0.0
    for index, row in enumerate(ordered):
        starts.append(at)
        at += row.duration
        if index + 1 == len(ordered):
            break
        following = ordered[index + 1]
        gap = (following.started_at - row.started_at).total_seconds() - row.duration
        if gap < -CLOCK_PRECISION_SECONDS:
            raise ValueError(f"{following.name} starts before {row.name} ends.")
        joins.append(
            CameraJoin(
                video_seconds=round(at, 3),
                gap_seconds=round(max(gap, 0.0), 3),
                is_break=gap <= max_gap,
            )
        )
    anchors: dict[str, float] = {}
    for part in parts:
        for row, start in zip(ordered, starts, strict=True):
            offset = (part.start_time - row.started_at).total_seconds()
            if 0 <= offset <= row.duration:
                anchors[str(part.id_uuid)] = round(start + offset, 3)
                break
    return CameraSync(
        joins=joins,
        breaks=[
            (join.video_seconds, join.gap_seconds) for join in joins if join.is_break
        ],
        anchors=anchors,
    )
