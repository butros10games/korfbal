"""Bounded close-up inference that separates opposing, overlapping bodies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from operator import itemgetter
import time
from typing import TYPE_CHECKING, Any, cast

from .clip_clothing import mixed, sample
from .clip_recovery import overlap
from .clip_signals import Teams, modules


if TYPE_CHECKING:
    from numpy.typing import NDArray


MAX_CROPS_PER_FRAME = 2
MIN_SCORE = 0.6
MAX_PAIR_IOU = 0.75
MIN_PAIR_IOU = 0.05
MIN_HEIGHT_RATIO = 0.65
MAX_HEIGHT_RATIO = 1.5
BUDGET_FRACTION = 0.4
PROBE_BUDGET_SHARE = 0.25
CROP_SIZE = 640
MIN_PARENT_SCORE = 0.5
FAILED_RETRY_SECONDS = 0.24
MAX_FAILURE_STREAK = 3
RECENT_SECONDS = 1.0
REGION_IOU = 0.5
EXISTING_PAIR_IOU = 0.25


@dataclass(frozen=True)
class OverlapFrame:
    """Share the detector, inference budget and cancellation/deadline boundary."""

    detector: object | None
    inference_seconds: float
    other_seconds: float
    stopped: Callable[[], bool]
    timestamp: float = 0


class OverlapRecovery:
    """Replace a merged proposal only with two separately supported detections."""

    def __init__(self) -> None:
        """Keep inference cost bounded across the entire processing section."""
        self.calls = 0
        self.seconds = 0.0
        self.probe_seconds = 0.0
        self.splits = 0
        self.recent: list[dict] = []
        self.observed_pairs: list[dict] = []

    def refine(
        self, raw: object, image: NDArray[Any], teams: Teams, frame: OverlapFrame
    ) -> object:
        """Bound local passes per frame under the shared budget and job deadline."""
        raw = cast("Any", raw)
        if frame.detector is None or frame.stopped():
            return raw
        rows = raw.boxes.cpu().numpy().data
        h, w = image.shape[:2]
        attempts = 0
        for index, row, box in self.regions(raw, rows, image, teams, frame.timestamp):
            returning = any(
                overlap(box, r["box"]) > REGION_IOU for r in self.observed_pairs
            )
            if (
                attempts >= MAX_CROPS_PER_FRAME
                or self.seconds + frame.other_seconds
                > frame.inference_seconds * BUDGET_FRACTION
            ):
                break
            # Leave most of the shared allowance available for observed pairs that
            # merge. Speculative mixed-colour regions must not spend it all first.
            if not returning and self.probe_seconds > (
                frame.inference_seconds * BUDGET_FRACTION * PROBE_BUDGET_SHARE
            ):
                continue
            if (
                raw.names[int(row[5])] not in {"player", "person"}
                or row[4] < MIN_PARENT_SCORE
            ):
                continue
            a, b, c, d = [float(v) for v in row[:4]]
            left, top = max(0, int(a - (c - a) * 0.65)), max(0, int(b - (d - b) * 0.35))
            right, bottom = (
                min(w, int(c + (c - a) * 0.65)),
                min(h, int(d + (d - b) * 0.35)),
            )
            region = (left, top, right, bottom)
            started = time.monotonic()
            self.calls += 1
            attempts += 1
            try:
                # The detector handles letterboxing, preserving source crop detail.
                _, np = modules()
                patch = np.full(
                    (max(right - left, bottom - top),) * 2 + (3,),
                    114,
                    dtype=image.dtype,
                )
                patch[: bottom - top, : right - left] = image[top:bottom, left:right]
                result = cast("Any", frame.detector).predict(
                    patch,
                    device="cpu",
                    imgsz=CROP_SIZE,
                    conf=0.1,
                    max_det=24,
                    verbose=False,
                )[0]
            finally:
                elapsed = time.monotonic() - started
                self.seconds += elapsed
                if not returning:
                    self.probe_seconds += elapsed
            if frame.stopped():
                return raw
            pair = self.separate(result, region, box, image, teams)
            previous = [r for r in self.recent if overlap(box, r["box"]) > REGION_IOU]
            prior = max(previous, key=itemgetter("time"), default={})
            failures = 0 if pair is not None else 1 + prior.get("failures", 0)
            self.recent.append({
                "box": box,
                "time": frame.timestamp,
                "split": pair is not None,
                "failures": min(failures, MAX_FAILURE_STREAK),
            })
            if pair is None:
                continue
            return self.replace(raw, rows, index, pair, image.shape)
        return raw

    def regions(
        self,
        raw: object,
        rows: NDArray[Any],
        image: NDArray[Any],
        teams: Teams,
        timestamp: float,
    ) -> list:
        """Back off failed crops so they cannot starve a recurring real overlap."""
        raw = cast("Any", raw)
        self.recent = [
            r for r in self.recent if 0 <= timestamp - r["time"] <= RECENT_SECONDS
        ]
        self.observed_pairs = [
            r
            for r in self.observed_pairs
            if 0 <= timestamp - r["time"] <= RECENT_SECONDS
        ]
        h, w = image.shape[:2]
        candidates = []
        for index, row in enumerate(rows):
            if (
                raw.names[int(row[5])] not in {"player", "person"}
                or row[4] < MIN_PARENT_SCORE
            ):
                continue
            a, b, c, d = [float(v) for v in row[:4]]
            box = [a / w, b / h, (c - a) / w, (d - b) / h]
            nearby = [r for r in self.recent if overlap(box, r["box"]) > REGION_IOU]
            if self.already_separated(rows, index, box, raw.names, (w, h)):
                self.observed_pairs = [
                    r
                    for r in self.observed_pairs
                    if overlap(box, r["box"]) <= REGION_IOU
                ]
                self.observed_pairs.append({"box": box, "time": timestamp})
                continue
            prior = max(nearby, key=itemgetter("time"), default=None)
            if (
                prior is not None
                and not prior["split"]
                and timestamp - prior["time"]
                < FAILED_RETRY_SECONDS * 2 ** max(0, prior.get("failures", 1) - 1)
            ):
                continue
            returning = max(
                (
                    r["time"]
                    for r in self.observed_pairs
                    if overlap(box, r["box"]) > REGION_IOU
                ),
                default=float("-inf"),
            )
            if returning != float("-inf") or mixed(teams, image, box):
                # Prefer a newly merged pair over repeatedly probing an ambiguous
                # region. Existing full-frame pairs supply this evidence for free.
                priority = (returning, any(r["split"] for r in nearby))
                candidates.append((priority, index, row, box))
        candidates.sort(key=itemgetter(0), reverse=True)
        return [(index, row, box) for _, index, row, box in candidates]

    @staticmethod
    def already_separated(
        rows: NDArray[Any], index: int, box: list, names: dict, dimensions: tuple
    ) -> bool:
        """Existing overlapping player boxes already provide both observations."""
        w, h = dimensions
        return any(
            i != index
            and names[int(row[5])] in {"player", "person"}
            and row[4] >= MIN_PARENT_SCORE
            and overlap(
                box,
                [row[0] / w, row[1] / h, (row[2] - row[0]) / w, (row[3] - row[1]) / h],
            )
            > EXISTING_PAIR_IOU
            for i, row in enumerate(rows)
        )

    def separate(
        self,
        result: object,
        region: tuple,
        parent: list,
        image: NDArray[Any],
        teams: Teams,
    ) -> list | None:
        """Validate two distinct bodies in a region selected by shirt/pair evidence."""
        result = cast("Any", result)
        h, w = image.shape[:2]
        left, top, right, bottom = region
        candidates = []
        for row in result.boxes.cpu().numpy().data:
            a, b, c, d, score, cls = [float(v) for v in row[:6]]
            if result.names[int(cls)] not in {"player", "person"} or score < MIN_SCORE:
                continue
            if a <= 1 or b <= 1 or c >= right - left - 1 or d >= bottom - top - 1:
                continue
            box = [(left + a) / w, (top + b) / h, (c - a) / w, (d - b) / h]
            if not MIN_HEIGHT_RATIO < box[3] / parent[3] < MAX_HEIGHT_RATIO:
                continue
            candidates.append((box, score))
        supported = []
        for (a, sa), (b, sb) in combinations(candidates, 2):
            if not MIN_PAIR_IOU < overlap(a, b) < MAX_PAIR_IOU:
                continue
            ca, _ = sample(teams, image, a, [b], wide=True)
            cb, _ = sample(teams, image, b, [a], wide=True)
            va, vb = teams.vote(ca), teams.vote(cb)
            if (va is None and vb is None) or (
                va is not None and vb is not None and va[0] == vb[0]
            ):
                continue
            if min(overlap(a, parent), overlap(b, parent)) < MIN_PAIR_IOU:
                continue
            supported.append([(a, sa), (b, sb)])
        return supported[0] if len(supported) == 1 else None

    def replace(
        self, raw: object, rows: NDArray[Any], index: int, pair: list, shape: tuple
    ) -> object:
        """Replace the parent while retaining unrelated detections and class IDs."""
        raw = cast("Any", raw)
        _, np = modules()
        h, w = shape[:2]
        cls = rows[index, 5]
        added = []
        for box, score in pair:
            # A separate full-frame box already representing this child is kept.
            if any(
                i != index
                and int(row[5]) == int(cls)
                and overlap(
                    box,
                    [
                        row[0] / w,
                        row[1] / h,
                        (row[2] - row[0]) / w,
                        (row[3] - row[1]) / h,
                    ],
                )
                > MAX_PAIR_IOU
                for i, row in enumerate(rows)
            ):
                continue
            x, y, bw, bh = box
            added.append([x * w, y * h, (x + bw) * w, (y + bh) * h, score, cls])
        new = raw.new()
        new.update(
            boxes=np.asarray(
                [row for i, row in enumerate(rows) if i != index] + added,
                dtype=np.float32,
            ).reshape(-1, 6)
        )
        new.overlap_indices = set(range(len(rows) - 1, len(rows) - 1 + len(added)))
        self.splits += 1
        return new

    def snapshot(self) -> dict:
        """Report detector work separately from identity accuracy."""
        return {
            "enabled": True,
            "crop_calls": self.calls,
            "crop_seconds": round(self.seconds, 3),
            "probe_seconds": round(self.probe_seconds, 3),
            "split_frames": self.splits,
        }
