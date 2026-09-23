"""Score reviewed tracking keyframes without confusing fewer IDs with accuracy."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
import importlib
import math

from .vision import iou


BOX_DIMENSIONS = 4
BOX_BOUND = 1.000001
MAX_TOLERANCE = 0.1
MIN_IOU = 0.5


def assignment(scores: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    """Maximize valid one-to-one matches before their overlap tie-breaker."""
    if not scores or not scores[0]:
        return []
    np = importlib.import_module("numpy")
    solver = importlib.import_module("scipy.optimize").linear_sum_assignment
    matrix = np.array(scores)
    rows, columns = solver(-matrix)
    return [
        (int(a), int(b)) for a, b in zip(rows, columns, strict=True) if matrix[a, b] > 0
    ]


def validate_frames(frames: list[dict]) -> None:
    """Reject duplicate observations and invalid geometry instead of skewing scores.

    Raises:
        ValueError: Timestamps, identities or geometry are invalid.

    """
    previous = -math.inf
    if not frames:
        raise ValueError("Supply at least one frame")
    for frame in frames:
        timestamp = frame["time_seconds"]
        if not math.isfinite(timestamp) or timestamp <= previous:
            raise ValueError("Frame timestamps must be finite and strictly increasing")
        previous = timestamp
        identities = set()
        for obj in frame["objects"]:
            identity = obj.get("track_id")
            if not isinstance(identity, str) or not identity or identity in identities:
                raise ValueError("Use unique nonempty track IDs within each frame")
            identities.add(identity)
            box = obj["bbox"]
            if (
                len(box) != BOX_DIMENSIONS
                or any(not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                or min(box[2:]) <= 0
                or box[0] + box[2] > BOX_BOUND
                or box[1] + box[3] > BOX_BOUND
            ):
                raise ValueError("Boxes must have positive normalized visible extents")


def match_objects(expected: list[dict], observed: list[dict]) -> dict[int, int]:
    """Prefer match cardinality, then IoU, within the same class."""
    bonus = min(len(expected), len(observed)) + 1
    scores = [
        [
            bonus + overlap
            if a["label"] == b["label"]
            and (overlap := iou(a["bbox"], b["bbox"])) >= MIN_IOU
            else 0
            for b in observed
        ]
        for a in expected
    ]
    return dict(assignment(scores))


class IdentityScore:
    """Keep ground-truth segments separate from the algorithm's camera decisions."""

    def __init__(self) -> None:
        """Initialize one clip; identities never carry across separate clips."""
        self.counts: Counter = Counter()
        self.overlaps: Counter = Counter()
        self.truth_ids: set = set()
        self.predicted_ids: set = set()
        self.previous: dict = {}
        self.owners: dict = {}
        self.team_pairs: Counter = Counter()
        self.events: list = []

    def observe(self, obj: dict, other: dict | None, segment: int, time: float) -> None:
        """Count switches through gaps and incorrect reuse on another known player."""
        identity = (segment, obj["label"], obj["track_id"])
        self.truth_ids.add(identity)
        track = (other["label"], other["track_id"]) if other else None
        last = self.previous.get(identity)
        if last is not None:
            self.counts["reference_pairs"] += 1
            if track is not None and last["track"] is not None:
                self.counts["matched_pairs"] += 1
                self.counts["stable_pairs"] += int(track == last["track"])
        if track is not None:
            self.matched(identity, track, last, time)
        self.previous[identity] = {
            "track": track,
            "last_matched": track or (last["last_matched"] if last else None),
        }
        if obj.get("team") in {"team_a", "team_b"}:
            self.counts["team_reference"] += 1
            if other and other.get("team") in {"team_a", "team_b"}:
                self.counts["team_assigned"] += 1
                self.team_pairs[obj["team"], other["team"]] += 1

    def matched(
        self, identity: tuple, track: tuple, last: dict | None, time: float
    ) -> None:
        """Preserve the last matched identity across missing keyframes."""
        self.overlaps[identity, track] += 1
        prior_track = last["last_matched"] if last else None
        if prior_track is not None and prior_track != track:
            self.event("id_switches", identity, time)
        if track in self.owners and self.owners[track] != identity:
            self.event("wrong_identity_reuses", identity, time)
        self.owners[track] = identity
        if last and last["track"] is None and prior_track is not None:
            self.counts["reappearances"] += 1
            self.counts["correct_reappearances"] += int(prior_track == track)

    def event(self, kind: str, identity: tuple, time: float) -> None:
        """Retain inspectable timestamps alongside aggregate counts."""
        self.counts[kind] += 1
        self.events.append({"time": time, "identity": identity[2], "kind": kind})

    def report(self, scope: str) -> dict:
        """Expose cohort recall, and IDF1 only with complete reference coverage."""
        counts = self.counts
        a, b = sorted(self.truth_ids), sorted(self.predicted_ids)
        scores = [[self.overlaps[x, y] for y in b] for x in a]
        identity_tp = sum(scores[i][j] for i, j in assignment(scores))
        direct = (
            self.team_pairs["team_a", "team_a"] + self.team_pairs["team_b", "team_b"]
        )
        flipped = (
            self.team_pairs["team_a", "team_b"] + self.team_pairs["team_b", "team_a"]
        )
        counts["identity_correct"] = identity_tp
        counts["team_correct"] = max(direct, flipped)
        counts["team_wrong"] = counts["team_assigned"] - counts["team_correct"]
        return {
            "scope": scope,
            "counts": dict(counts),
            "identity_recall": identity_tp / max(1, counts["reference"]),
            "stable_pair_recall": counts["stable_pairs"]
            / max(1, counts["reference_pairs"]),
            "idf1_keyframes": (
                2 * identity_tp / max(1, counts["reference"] + counts["predicted"])
                if scope == "complete"
                else None
            ),
            "team_mapping": "same" if direct >= flipped else "swapped",
            "events": self.events,
        }


def score_clip(
    references: list[dict],
    predictions: list[dict],
    *,
    scope: str,
    tolerance: float = 0.041,
) -> dict:
    """Score class-aware IoU ≥ 0.5 matches on reviewed tracking keyframes.

    Missing frames count as misses. Every reference remains in the denominator.
    Team names get one permutation for the clip, never one per frame or camera
    cut. A subject cohort cannot measure full-frame false positives or IDF1.
    These sparse-keyframe diagnostics are not dense-video MOTA/HOTA scores.

    Raises:
        ValueError: Inputs are malformed or cannot align one-to-one.

    """
    validate_frames(references)
    validate_frames(predictions)
    if scope not in {"complete", "subjects"}:
        raise ValueError("Declare complete or subjects reference coverage")
    if scope == "complete" and not all(f.get("complete") for f in references):
        raise ValueError("Complete scoring requires explicitly complete references")
    if not math.isfinite(tolerance) or not 0 <= tolerance <= MAX_TOLERANCE:
        raise ValueError("Choose a timestamp tolerance between zero and 0.1 seconds")
    score = IdentityScore()
    used_frames = set()
    for ref in references:
        nearest = min(
            range(len(predictions)),
            key=lambda i: abs(predictions[i]["time_seconds"] - ref["time_seconds"]),
        )
        pred = predictions[nearest]
        aligned = abs(pred["time_seconds"] - ref["time_seconds"]) <= tolerance
        if aligned and nearest in used_frames:
            raise ValueError("Reference frames must align to different predictions")
        if aligned:
            used_frames.add(nearest)
        observed = pred["objects"] if aligned else []
        expected = ref["objects"]
        matched = match_objects(expected, observed)
        score.counts.update(
            reference=len(expected),
            predicted=len(observed),
            matched=len(matched),
            missed=len(expected) - len(matched),
        )
        if scope == "complete":
            score.counts["extra"] += len(observed) - len(matched)
        score.predicted_ids.update((o["label"], o["track_id"]) for o in observed)
        for index, obj in enumerate(expected):
            other = observed[matched[index]] if index in matched else None
            score.observe(obj, other, ref.get("segment", 0), ref["time_seconds"])
    return score.report(scope)
