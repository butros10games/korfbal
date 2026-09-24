"""Jointly stitch clean tracklet endpoints within a completed camera segment.

Weak observations do not define a tracklet's lifetime. Keep corrections scoped
per frame so a reused detector stream is never renamed across unrelated bodies.
"""

from __future__ import annotations

from collections import defaultdict
import math
from operator import itemgetter
from statistics import median
from typing import TYPE_CHECKING

from .clip_evaluation import assignment
from .clip_refinement_spans import MIN_ANCHORS, reachable, unique_corrections, view_cost


if TYPE_CHECKING:
    from collections.abc import Callable

    from .clip_refinement import IdentityRefiner

MAX_GAP = 4.0
MAX_COST = 0.78
MIN_MARGIN = 0.12
MAX_APPEARANCE = 0.32
ENDPOINT_SAMPLES = 5
MAX_GROUPS = 128
GALLERY_SIZE = 24
MAX_CONTINUOUS_GAP = 0.8
MAX_ENDPOINT_OVERLAP = 0.24


def solve(costs: list[list[float]], forbidden: tuple | None = None) -> tuple:
    """Allow unmatched births/deaths; never force a fixed number of identities."""
    size = len(costs)
    scores = [
        [
            max(0.0, MAX_COST - cost) if (i, j) != forbidden else 0.0
            for j, cost in enumerate(row)
        ]
        + [0.0] * size
        for i, row in enumerate(costs)
    ]
    pairs = [(i, j) for i, j in assignment(scores) if j < size]
    return pairs, sum(scores[i][j] for i, j in pairs)


def histories(rows: list, links: list, corrections: list) -> dict:
    """Use original clean samples under already resolved replay identities."""
    aliases = {link["from_track_id"]: link for link in links}
    scoped = {(c["time_seconds"], c["from_track_id"]): c for c in corrections}
    groups: dict[str, list] = defaultdict(list)
    superseded = {
        (c["time_seconds"], c["superseded_track_id"])
        for c in corrections
        if c.get("superseded_track_id")
    }
    for row in rows:
        if (row["time"], row["id"]) in superseded:
            continue
        link = scoped.get((row["time"], row["id"])) or aliases.get(row["id"])
        name = link["to_track_id"] if link else row["id"]
        groups[name].append({
            **row,
            "canonical": name,
            "display_id": link.get("display_id", row["display_id"])
            if link
            else row["display_id"],
        })
    runs = {}
    for name, stream in groups.items():
        parts: list[list] = []
        for row in sorted(stream, key=itemgetter("time")):
            if not parts or row["time"] - parts[-1][-1]["time"] > MAX_CONTINUOUS_GAP:
                parts.append([])
            parts[-1].append(row)
        for index, part in enumerate(parts):
            runs[name if index == 0 else (name, index)] = part
    return runs


def cost(
    refiner: IdentityRefiner,
    before: list,
    after: list,
    geometry_before: list | None = None,
    geometry_after: list | None = None,
) -> float:
    """Both motion directions, multiple clothing views and team evidence must agree."""
    a, b = before[-ENDPOINT_SAMPLES:], after[:ENDPOINT_SAMPLES]
    gap = b[0]["time"] - a[-1]["time"]
    if (
        not 0 < gap <= MAX_GAP
        or len({(r["segment"], r["epoch"]) for r in a + b}) != 1
        or not reachable(a[-1], b[0])
    ):
        return math.inf
    teams = {r["sample"]["shirt_team"] for r in a + b} - {"unknown"}
    if len(teams) != 1:
        return math.inf
    left, right = [r["sample"] for r in a], [r["sample"] for r in b]
    motion = refiner.motion_error(left, right)
    if geometry_before and geometry_after:
        motion = min(motion, body_motion(refiner, geometry_before, geometry_after))
    appearance = min(
        max(view_cost(refiner, left, right), view_cost(refiner, right, left)),
        gallery_cost(refiner, before, after),
    )
    if motion > 1 or appearance > MAX_APPEARANCE:
        return math.inf
    return 0.55 * motion + 0.45 * appearance / MAX_APPEARANCE


def gallery_cost(refiner: IdentityRefiner, before: list, after: list) -> float:
    """Require repeated matching views rather than averaging front and back poses."""
    a, b = diverse_views(refiner, before), diverse_views(refiner, after)
    distances = [
        sorted(
            min(
                torso_distance(refiner, left["sample"], right["sample"]),
                refiner.clothing_error([left["sample"]], [right["sample"]]),
            )
            for left in a
        )[1]
        for right in b
    ]
    return median(sorted(distances)[:MIN_ANCHORS])


def diverse_views(refiner: IdentityRefiner, rows: list) -> list:
    """Retain pairs of clean views across appearance changes, not fixed time strides."""
    if len(rows) <= GALLERY_SIZE:
        return rows
    np = refiner.np
    features = np.sqrt(np.asarray([r["sample"]["appearance"] for r in rows])).reshape(
        len(rows), -1
    )
    distances = np.full(len(rows), np.inf)
    selected = []
    for _ in range(GALLERY_SIZE // 2):
        seed = int(np.argmax(distances))
        delta = np.linalg.norm(features - features[seed], axis=1)
        available = [int(i) for i in np.argsort(delta) if int(i) not in selected]
        selected.extend(available[:2])
        distances = np.minimum(distances, delta)
        distances[selected] = -1
    return [rows[i] for i in sorted(selected)]


def torso_distance(refiner: IdentityRefiner, left: dict, right: dict) -> float:
    """Prioritize jersey bands over variable legs, floor and stride poses."""
    np = refiner.np
    a, b = np.asarray(left["appearance"]), np.asarray(right["appearance"])
    distances = np.sqrt(np.square(np.sqrt(a) - np.sqrt(b)).sum(axis=1) / 2)
    return float(np.average(distances, weights=[0.5, 0.5, 0.0, 0.0]))


def body_motion(refiner: IdentityRefiner, before: list, after: list) -> float:
    """Visible bodies can support motion even when their shirts are occluded."""
    # Discard a weak prefix that coexisted with the established body. A reused
    # detector run may only inherit its identity after that coexistence ends.
    if before[-1]["time"] - after[0]["time"] > MAX_ENDPOINT_OVERLAP:
        after = [r for r in after if r["time"] > before[-1]["time"]]
    if not after:
        return math.inf
    start = after[0]["time"]
    a = [r for r in before if r["time"] < start][-ENDPOINT_SAMPLES:]
    b = after[:ENDPOINT_SAMPLES]
    if min(len(a), len(b)) < MIN_ANCHORS:
        return math.inf

    def geometry(rows: list) -> list:
        return [
            {
                "time": r["time"],
                "image": r["image"],
                "height": r["height"],
                "epoch": r["epoch"],
                "court": None,
                "court_key": None,
            }
            for r in rows
        ]

    return refiner.motion_error(geometry(a), geometry(b))


def reconcile(
    refiner: IdentityRefiner,
    links: list,
    corrections: list,
    stopped: Callable[[], bool] | None = None,
) -> list | None:
    """Select a joint path cover and abstain if an alternative is nearly as good."""
    if refiner.spans.truncated:
        return []
    groups = histories(refiner.spans.rows, links, corrections)
    clean = {
        key: [r for r in rows if r["sample"] is not None and not r["uncertain"]]
        for key, rows in groups.items()
    }
    clean = {key: rows for key, rows in clean.items() if len(rows) >= MIN_ANCHORS}
    sections: dict[tuple, list] = defaultdict(list)
    for key, rows in clean.items():
        if len({(r["segment"], r["epoch"]) for r in rows}) == 1:
            sections[rows[0]["segment"], rows[0]["epoch"]].append(key)
    output = []
    occupied: dict[float, set] = defaultdict(set)
    for rows in groups.values():
        for row in rows:
            occupied[row["time"]].add(row["canonical"])
    for names in sections.values():
        if stopped and stopped():
            return None
        if len(names) > MAX_GROUPS:
            refiner.truncated = True
            continue
        accepted = select_paths(refiner, names, clean, groups, stopped)
        if accepted is None:
            return None
        output.extend(corrections_for(accepted, clean, groups, occupied))
    return unique_corrections(output)


def select_paths(
    refiner: IdentityRefiner,
    names: list,
    clean: dict,
    groups: dict,
    stopped: Callable[[], bool] | None,
) -> dict | None:
    """Measure confidence against the best complete alternative assignment."""
    costs = []
    for a in names:
        if stopped and stopped():
            return None
        costs.append([
            cost(refiner, clean[a], clean[b], groups[a], groups[b]) for b in names
        ])
    pairs, total = solve(costs)
    accepted = {}
    for i, j in pairs:
        if stopped and stopped():
            return None
        _, alternative = solve(costs, (i, j))
        if total - alternative >= MIN_MARGIN:
            accepted[names[j]] = names[i]
    return accepted


def corrections_for(accepted: dict, clean: dict, groups: dict, occupied: dict) -> list:
    """Keep simultaneous people separate and respect contradictory visible shirts."""
    output = []
    for target, predecessor in accepted.items():
        source = predecessor
        while source in accepted:
            source = accepted[source]
        root = clean[source][0]
        canonical = root.get("canonical", source)
        team = next(
            (
                r["sample"]["shirt_team"]
                for r in clean[source]
                if r["sample"]["shirt_team"] != "unknown"
            ),
            "unknown",
        )
        boundary = groups[predecessor][-1]["time"]
        for row in groups[target]:
            if row["time"] <= boundary:
                continue
            # Retain both bodies whenever the proposed identity already exists.
            shirt = row["sample"]["shirt_team"] if row["sample"] else "unknown"
            if canonical in occupied[row["time"]] or shirt not in {"unknown", team}:
                continue
            output.append({
                "time_seconds": row["time"],
                "from_track_id": row["id"],
                "to_track_id": canonical,
                "display_id": root["display_id"],
                "team": team,
            })

    return output
