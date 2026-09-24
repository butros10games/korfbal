"""Backfill short observation runs bracketed by the same established identity.

A native tracker can reuse one provisional ID for different bodies. Corrections
are therefore scoped to individual frame observations, never a blanket rename.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
import math
from statistics import median
from typing import TYPE_CHECKING

from . import clip_refinement_ownership as ownership


if TYPE_CHECKING:
    from collections.abc import Callable

    from .clip_refinement import IdentityRefiner

MAX_OBSERVATIONS = 30_000
MAX_GAP = 4.0
MAX_SAMPLE_GAP = 0.24
MIN_ANCHORS = 3
ENDPOINT_SAMPLES = 5
MAX_APPEARANCE = 0.32
MIN_MARGIN = 0.06
MIN_SIZE_RATIO = 0.65
MAX_SIZE_RATIO = 1.55
MIN_VIEW_SUPPORT = 2


class ObservationSpans:
    """Bounded, image-free evidence for completed replay corrections."""

    def __init__(self) -> None:
        """Start an independent processing section."""
        self.rows: list[dict] = []
        self.truncated = False

    def observe(self, row: dict) -> None:
        """Retain geometry and clothing, never image pixels."""
        if len(self.rows) >= MAX_OBSERVATIONS:
            self.truncated = True
            return
        self.rows.append(row)

    def finish(
        self,
        refiner: IdentityRefiner,
        links: list[dict],
        stopped: Callable[[], bool] | None,
    ) -> list[dict] | None:
        """Require two-sided clothing, bounded movement and unique candidates."""
        if self.truncated:
            return []
        corrections: list[dict] = []
        # One follow-up pass can use an independently recovered view at a gap edge.
        # Keep refinement bounded; never repeatedly spread an inferred identity.
        for _ in range(2):
            result = self.pass_links(refiner, links, corrections, stopped)
            if result is None:
                return None
            known = {(c["time_seconds"], c["from_track_id"]) for c in corrections}
            corrections.extend(
                c
                for c in result
                if (c["time_seconds"], c["from_track_id"]) not in known
            )
        bridges = ownership.close_gaps(self.rows, links, corrections, stopped)
        if bridges is None:
            return None
        return unique_corrections(corrections + bridges)

    def pass_links(
        self,
        refiner: IdentityRefiner,
        links: list[dict],
        prior: list[dict],
        stopped: Callable[[], bool] | None,
    ) -> list[dict] | None:
        """Resolve one generation from frozen anchors, without in-pass feedback."""
        aliases = {link["from_track_id"]: link["to_track_id"] for link in links}
        scoped = {(c["time_seconds"], c["from_track_id"]): c for c in prior}

        def canonical(row: dict) -> str:
            return scoped.get((row["time"], row["id"]), {}).get(
                "to_track_id", aliases.get(row["id"], row["id"])
            )

        histories: dict[str, list] = defaultdict(list)
        frames: dict[float, list] = defaultdict(list)
        streams: dict[str, list] = defaultdict(list)
        superseded = {
            (c["time_seconds"], c["superseded_track_id"])
            for c in prior
            if c.get("superseded_track_id")
        }
        for row in self.rows:
            if (row["time"], row["id"]) in superseded:
                continue
            frames[row["time"]].append(row)
            streams[row["id"]].append(row)
            histories[canonical(row)].append(
                corrected_anchor(row, scoped.get((row["time"], row["id"])))
            )
        gaps = anchor_gaps(histories)
        corrections = []
        for stream in streams.values():
            for run in observation_runs(stream):
                if stopped and stopped():
                    return None
                candidates = run_candidates(refiner, run, gaps, canonical(run[0]))
                if not candidates:
                    continue
                _, identity, display, team = candidates[0]
                # Keep competing people distinct unless the weaker observation
                # independently satisfies the strict occluded-fragment checks.
                for row in run:
                    rivals = [
                        other
                        for other in frames[row["time"]]
                        if canonical(other) == identity
                    ]
                    if rivals and (
                        len(rivals) != 1
                        or not ownership.supersedes(row, rivals[0], frames[row["time"]])
                    ):
                        continue
                    corrections.append({
                        "time_seconds": row["time"],
                        "from_track_id": row["id"],
                        "to_track_id": identity,
                        "display_id": display,
                        "team": team,
                        **({"superseded_track_id": rivals[0]["id"]} if rivals else {}),
                    })
        return unique_corrections(corrections)


def reachable(anchor: dict, row: dict) -> bool:
    """Use camera-compensated body sizes rather than clip-specific pixel gates."""
    dt = abs(row["time"] - anchor["time"])
    radius = (anchor["height"] + row["height"]) / 2 * (0.3 + 0.8 * dt)
    ratio = row["height"] / max(anchor["height"], 1e-9)
    return (
        MIN_SIZE_RATIO < ratio < MAX_SIZE_RATIO
        and math.dist(row["image"], anchor["image"]) <= radius
    )


def anchor_gaps(histories: dict[str, list]) -> list:
    """Find short gaps between mature observations in one camera epoch."""
    gaps = []
    for identity, rows in histories.items():
        history = anchor_history(rows)
        for index, (before, after) in enumerate(pairwise(history)):
            dt = after["time"] - before["time"]
            if not MAX_SAMPLE_GAP < dt <= MAX_GAP:
                continue
            a = history[max(0, index + 1 - ENDPOINT_SAMPLES) : index + 1]
            b = history[index + 1 : index + 1 + ENDPOINT_SAMPLES]
            if min(len(a), len(b)) < MIN_ANCHORS:
                continue
            if len({(r["segment"], r["epoch"]) for r in a + b}) != 1:
                continue
            gaps.append((identity, a, b))
    return gaps


def observation_runs(stream: list[dict]) -> list[list]:
    """Split reused native identities at interruptions and camera boundaries."""
    runs: list[list] = []
    for row in stream:
        if (
            not runs
            or row["time"] - runs[-1][-1]["time"] > MAX_SAMPLE_GAP
            or (row["segment"], row["epoch"])
            != (runs[-1][-1]["segment"], runs[-1][-1]["epoch"])
        ):
            runs.append([])
        runs[-1].append(row)
    return runs


def bridge_cost(
    refiner: IdentityRefiner, run: list, samples: list, a: list, b: list
) -> float:
    """Reject runs outside the anchors or unsupported by either endpoint."""
    if not a[-1]["time"] < run[0]["time"] <= run[-1]["time"] < b[0]["time"]:
        return math.inf
    if (run[0]["segment"], run[0]["epoch"]) != (a[-1]["segment"], a[-1]["epoch"]):
        return math.inf
    left, right = [r["sample"] for r in a], [r["sample"] for r in b]
    cost = max(view_cost(refiner, left, samples), view_cost(refiner, right, samples))
    if any(not reachable(anchor, row) for anchor in (a[-1], b[0]) for row in run):
        return math.inf
    return cost


def unique_corrections(corrections: list[dict]) -> list[dict]:
    """Reject simultaneous provisional runs converging on the same identity."""
    counts: dict[tuple, int] = defaultdict(int)
    for correction in corrections:
        counts[correction["time_seconds"], correction["to_track_id"]] += 1
    return [c for c in corrections if counts[c["time_seconds"], c["to_track_id"]] == 1]


def run_candidates(
    refiner: IdentityRefiner, run: list, gaps: list, canonical: str
) -> list:
    """Compare competing gap owners, abstaining when the best is ambiguous."""
    samples = [r["sample"] for r in run if r["sample"] is not None]
    if not samples:
        return []
    votes = [s["shirt_team"] for s in samples if s["shirt_team"] != "unknown"]
    observed_teams = set(votes) if len(votes) >= MIN_ANCHORS else set()
    candidates = []
    for identity, a, b in gaps:
        if identity == canonical:
            continue
        cost = bridge_cost(refiner, run, samples, a, b)
        if math.isfinite(cost):
            teams = {r["sample"]["team"] for r in a + b} - {"unknown"}
            if len(teams) > 1 or (
                len(observed_teams) == 1 and teams and observed_teams != teams
            ):
                continue
            candidates.append((
                cost,
                identity,
                a[-1]["display_id"],
                next(iter(teams), "unknown"),
            ))
    candidates.sort()
    if not candidates or candidates[0][0] > MAX_APPEARANCE:
        return []
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < MIN_MARGIN:
        return []
    return candidates


def anchor_history(rows: list[dict]) -> list[dict]:
    """Exclude uncertain and occluded observations from established anchors."""
    return [r for r in rows if r["sample"] is not None and not r["uncertain"]]


def view_cost(refiner: IdentityRefiner, anchors: list, samples: list) -> float:
    """Require matching views in multiple anchor frames without averaging poses."""
    return min(
        refiner.clothing_error(anchors, samples),
        median(
            sorted(refiner.clothing_error([anchor], [sample]) for anchor in anchors)[
                MIN_VIEW_SUPPORT - 1
            ]
            for sample in samples
        ),
    )


def corrected_anchor(row: dict, correction: dict | None) -> dict:
    """Use the resolved identity label while retaining original clothing evidence."""
    if correction is None:
        return row
    return {
        **row,
        "uncertain": False,
        "display_id": correction["display_id"],
        "sample": {**row["sample"], "team": correction["team"]}
        if row["sample"]
        else None,
    }
