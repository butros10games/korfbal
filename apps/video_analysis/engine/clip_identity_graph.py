"""Experimental completed-section identity graph, independent of replay repairs.

Detector streams are observations, not people. Split them into conservative
tracklets, solve one temporal path cover, and expose ambiguous spans for review.
This module is deliberately not enabled in the production publication path.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
import math
from operator import itemgetter
from typing import TYPE_CHECKING

from .clip_evaluation import assignment
from .clip_segment_reconciliation import MAX_COST, MIN_MARGIN, cost


MIN_SAMPLES = 3


if TYPE_CHECKING:
    from .clip_refinement import IdentityRefiner


@dataclass(frozen=True)
class GraphPolicy:
    """Versioned, serializable limits; never encode a roster or scene identity."""

    sample_gap: float = 0.24
    minimum_samples: int = MIN_SAMPLES
    maximum_nodes: int = 128
    minimum_margin: float = MIN_MARGIN
    maximum_cost: float = MAX_COST

    def __post_init__(self) -> None:
        """Reject policies that could accept missing evidence.

        Raises:
            ValueError: Limits must be finite, positive and sufficiently supported.

        """
        if (
            self.minimum_samples < MIN_SAMPLES
            or self.maximum_nodes < 1
            or any(
                not math.isfinite(value) or value <= 0
                for value in (self.sample_gap, self.minimum_margin, self.maximum_cost)
            )
        ):
            raise ValueError("Graph policy requires positive limits and three samples")


def trusted(row: dict) -> bool:
    """Only original clear observations can support appearance identity evidence."""
    return bool(row.get("sample") and not row.get("uncertain"))


def team(rows: list) -> str:
    """Contradictory known shirts cannot establish one person's team."""
    votes = {r["sample"]["shirt_team"] for r in rows if trusted(r)} - {"unknown"}
    return next(iter(votes)) if len(votes) == 1 else "unknown"


def tracklets(rows: list, policy: GraphPolicy) -> list[dict]:
    """Split reused streams at gaps, camera boundaries and contradictory evidence."""
    streams: dict[tuple, list] = defaultdict(list)
    for row in rows:
        streams[row["segment"], row["epoch"], row["id"]].append(row)
    output = []
    for key, stream in sorted(streams.items()):
        parts: list[list] = []
        for row in sorted(stream, key=itemgetter("time")):
            if not parts or separated(parts[-1][-1], row, policy):
                parts.append([])
            parts[-1].append(row)
        for index, part in enumerate(parts):
            output.append({
                "key": f"{key[0]}:{key[1]}:{key[2]}:{index}",
                "section": key[:2],
                "rows": part,
                "team": team(part),
                "clean": [r for r in part if trusted(r)],
            })
    return sorted(output, key=lambda node: (node["rows"][0]["time"], node["key"]))


def separated(previous: dict, row: dict, policy: GraphPolicy) -> bool:
    """Reject unsupported transitions even when the detector ID stays the same."""
    if row["time"] - previous["time"] > policy.sample_gap:
        return True
    if trusted(row) and trusted(previous):
        a, b = row["sample"]["shirt_team"], previous["sample"]["shirt_team"]
        if a != b and "unknown" not in {a, b}:
            return True
    radius = (row["height"] + previous["height"]) * 0.5
    return math.dist(row["image"], previous["image"]) > radius


def edge_cost(
    refiner: IdentityRefiner, left: dict, right: dict, policy: GraphPolicy
) -> float:
    """Require nonoverlapping bodies and independent compatible evidence."""
    if (
        left["section"] != right["section"]
        or left["rows"][-1]["time"] >= right["rows"][0]["time"]
        or min(len(left["clean"]), len(right["clean"])) < policy.minimum_samples
        or left["team"] == "unknown"
        or left["team"] != right["team"]
    ):
        return math.inf
    return cost(refiner, left["clean"], right["clean"], left["rows"], right["rows"])


def solve(matrix: list, policy: GraphPolicy, forbidden: tuple | None = None) -> tuple:
    """Allow births and deaths under the exact recorded policy."""
    size = len(matrix)
    scores = [
        [
            max(0.0, policy.maximum_cost - value) if (i, j) != forbidden else 0.0
            for j, value in enumerate(row)
        ]
        + [0.0] * size
        for i, row in enumerate(matrix)
    ]
    pairs = [(i, j) for i, j in assignment(scores) if j < size]
    return pairs, sum(scores[i][j] for i, j in pairs)


def assign_section(
    refiner: IdentityRefiner,
    nodes: list,
    policy: GraphPolicy,
    stopped: Callable[[], bool],
) -> tuple | None:
    """Compare complete competing assignments, including the unmatched option."""
    matrix = []
    for left in nodes:
        if stopped():
            return None
        matrix.append([edge_cost(refiner, left, right, policy) for right in nodes])
    pairs, optimum = solve(matrix, policy)
    accepted, decisions = {}, []
    for i, j in pairs:
        if stopped():
            return None
        _, alternative = solve(matrix, policy, (i, j))
        margin = optimum - alternative
        approved = (
            matrix[i][j] < policy.maximum_cost and margin >= policy.minimum_margin
        )
        decisions.append({
            "from_tracklet": nodes[i]["key"],
            "to_tracklet": nodes[j]["key"],
            "cost": matrix[i][j],
            "alternative_margin": margin,
            "accepted": approved,
            "reason": "supported" if approved else "ambiguous",
        })
        if approved:
            accepted[nodes[j]["key"]] = nodes[i]["key"]
    return accepted, decisions


def resolve(
    refiner: IdentityRefiner,
    *,
    policy: GraphPolicy | None = None,
    stopped: Callable[[], bool] | None = None,
) -> dict:
    """Return an auditable alternative; never consume old aliases or edit frames."""
    policy = policy or GraphPolicy()
    stopped = stopped or (lambda: False)
    report = {
        "version": 1,
        "algorithm": "tracklet_graph_v1",
        "status": "completed",
        "experimental": True,
        "review_only": True,
        "policy": asdict(policy),
        "links": [],
        "frame_links": [],
        "decisions": [],
        "review_queue": [],
        "tracklets": [],
    }
    if refiner.spans.truncated or refiner.truncated:
        return {**report, "status": "insufficient_evidence"}
    groups: dict[tuple, list] = defaultdict(list)
    for node in tracklets(refiner.spans.rows, policy):
        groups[node["section"]].append(node)
    for section, nodes in groups.items():
        if stopped():
            return {
                **report,
                "status": "interrupted",
                "links": [],
                "frame_links": [],
                "decisions": [],
                "review_queue": [],
                "tracklets": [],
            }
        supported = [
            node
            for node in nodes
            if len(node["clean"]) >= policy.minimum_samples
            and node["team"] != "unknown"
        ]
        if len(supported) > policy.maximum_nodes:
            # Do not silently treat an unprocessed section as a successful result.
            report["status"] = "insufficient_evidence"
            report["review_queue"].append({
                "section": section,
                "reason": "capacity",
                "tracklets": len(nodes),
            })
            continue
        result = assign_section(refiner, supported, policy, stopped)
        if result is None:
            return {
                **report,
                "status": "interrupted",
                "links": [],
                "frame_links": [],
                "decisions": [],
                "review_queue": [],
                "tracklets": [],
            }
        accepted, decisions = result
        report["decisions"].extend(decisions)
        publish_section(report, nodes, accepted)
    if report["status"] != "completed":
        report["frame_links"] = []
    return report


def publish_section(report: dict, nodes: list, accepted: dict) -> None:
    """Identity namespace belongs to graph paths, never to recycled detector IDs."""
    roots = {}
    for node in nodes:
        root = node["key"]
        while root in accepted:
            root = accepted[root]
        roots[node["key"]] = root
    numbers = {
        root: index + 1 for index, root in enumerate(dict.fromkeys(roots.values()))
    }
    linked = set(accepted) | set(accepted.values())
    for node in nodes:
        identity = f"graph:{roots[node['key']]}"
        start, end = node["rows"][0]["time"], node["rows"][-1]["time"]
        metadata = {
            "tracklet": node["key"],
            "identity": identity,
            "start": start,
            "end": end,
            "team": node["team"],
        }
        report["tracklets"].append(metadata)
        if node["key"] not in linked or len(node["clean"]) < len(node["rows"]):
            report["review_queue"].append({
                **metadata,
                "reason": "weak_observations"
                if len(node["clean"]) < len(node["rows"])
                else "unlinked_tracklet",
            })
        for row in node["rows"]:
            report["frame_links"].append({
                "time_seconds": row["time"],
                "from_track_id": row["id"],
                "to_track_id": identity,
                "display_id": numbers[roots[node["key"]]],
                "team": node["team"],
            })
