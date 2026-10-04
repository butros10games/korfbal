"""Conservative match-wide identities over immutable shot-level trajectories.

This layer consumes flattened refinement aliases; it never changes detections or
fits appearance classes from human names. A constrained global partition keeps
unmatched fragments private. Confidence bounds calibrated pair evidence; it is
not a claim of whole-match or new-camera identification accuracy.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
import importlib
from itertools import combinations
import math
import time
from typing import Any


VERSION = 1
MAX_FRAGMENTS = 256
MAX_GALLERY_SAMPLES = 6
SOLVER_SECONDS = 15.0
MAX_SOLVER_ROUNDS = 24
BINARY_SELECTED = 0.5
MIN_CONFIDENCE = 0.98
TEAMS = ("team_a", "team_b")


@dataclass(frozen=True)
class Anchor:
    """A verified human name on a shot identity, optionally a roster number."""

    identity: str
    player_id: str
    team: str
    number: str | None = None
    source: str = "human_anchor"
    confidence: float = 1.0
    display_id: str | None = None
    # A carried automatic name: the kit orientation it was given under
    # ("kept", "swapped", or "any" for a view without a kit tag); ``None``
    # for a confirmation, or an older view that recorded none.
    named_under: str | None = None


@dataclass(frozen=True)
class NumberEvidence:
    """One independent visibility episode from a calibrated number reader.

    Adjacent frames must share an episode ID. Probabilities include unreadable
    mass through readability; uncalibrated readers are ignored entirely.
    """

    episode: str
    probabilities: dict[str, float]
    readability: float
    calibrated: bool = False


@dataclass
class Fragment:
    """One shot-level identity; descriptors are pure-tracklet prototypes."""

    identity: str
    shot: str
    team: str
    times: set[float]
    vectors: list[Any] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    numbers: list[NumberEvidence] = field(default_factory=list)
    replay: bool = False
    reference: str | None = None
    court: tuple[float, float] | None = None
    weight: int = 1
    conflicted: bool = False
    reference_verified: bool = False
    samples: list[Any] = field(default_factory=list)
    representative_track_id: str | None = None
    # Placed and on-court observations (``clip_live_play``); (0, 0) is unplaced.
    placement: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class Calibration:
    """Frozen monotone bins of cosine similarity versus same-person precision.

    Only a calibration with provenance enables automatic joins. Each probability
    must be evaluated on independent identities; it is never a softmax score.
    """

    provenance: str
    bins: tuple[tuple[float, float], ...]

    def probability(self, similarity: float) -> float:
        """Return the measured precision of the highest reached similarity bin."""
        result = 0.0
        for boundary, probability in self.bins:
            if similarity >= boundary:
                result = probability
        return result


def overlap(first: Fragment, second: Fragment) -> bool:
    """Replay video time is not live match time; exclude it from occupancy."""
    return not (first.replay or second.replay) and bool(first.times & second.times)


def compatible(first: Fragment, second: Fragment, anchors: dict[str, Anchor]) -> bool:
    """Check whole-cluster pair constraints, including verified anchor conflicts."""
    a, b = anchors.get(first.identity), anchors.get(second.identity)
    if first.conflicted or second.conflicted or overlap(first, second):
        return False
    if a and b and (a.player_id != b.player_id or a.team != b.team):
        return False
    if a and b and a.number and b.number and a.number != b.number:
        return False
    teams = {a.team if a else first.team, b.team if b else second.team} - {"unknown"}
    return len(teams) <= 1


def similarity(first: Fragment, second: Fragment) -> float | None:
    """Compare diverse pure-tracklet views in the same fitted appearance space."""
    if not first.vectors or not second.vectors:
        return None
    # A contaminated view must not get six independent votes. One best matching
    # view per side, then a median, rewards consistent rather than single hits.
    scores = [[float(a @ b) for b in second.vectors] for a in first.vectors]
    best = [max(row) for row in scores]
    best += [max(row[j] for row in scores) for j in range(len(second.vectors))]
    return sorted(best)[len(best) // 2]


def number_agreement(first: Fragment, second: Fragment) -> float | None:
    """Temper correlated number evidence to one distribution per episode."""

    def distribution(piece: Fragment) -> dict[str, float]:
        episodes = {n.episode: n for n in piece.numbers if n.calibrated}
        if not episodes:
            return {}
        total: dict[str, float] = defaultdict(float)
        for reading in episodes.values():
            for number, probability in reading.probabilities.items():
                total[number] += probability * reading.readability / len(episodes)
        return dict(total)

    a, b = distribution(first), distribution(second)
    return (
        sum(value * b.get(number, 0.0) for number, value in a.items())
        if a and b
        else None
    )


def evidence(
    first: Fragment, second: Fragment, calibration: Calibration | None
) -> float:
    """Use only calibrated evidence; court is a soft, verified-reference prior."""
    visual = similarity(first, second)
    probability = (
        calibration.probability(visual) if calibration and visual is not None else 0.0
    )
    number = number_agreement(first, second)
    if number is not None:
        probability = max(probability, number)
    verified = first.reference_verified and second.reference_verified
    if (
        verified
        and first.reference
        and first.reference == second.reference
        and first.court
        and second.court
    ):
        # No hard zone membership: ends change at goals/half-time and players move.
        gap = nearest_gap(first.times, second.times)
        excess = max(0.0, math.dist(first.court, second.court) - 7.5 * gap - 3.0)
        probability *= math.exp(-excess / 20.0)
    return min(1.0, max(0.0, probability))


def validate_anchors(
    fragments: list[Fragment], anchors: list[Anchor]
) -> dict[str, Anchor]:
    """Reject inconsistent verified inputs instead of silently overriding them.

    Raises:
        ValueError: An anchor is missing, duplicated or contradicts another anchor.

    """
    found = {piece.identity: piece for piece in fragments}
    by_id: dict[str, Anchor] = {}
    for anchor in anchors:
        if (
            anchor.identity not in found
            or anchor.team not in TEAMS
            or not anchor.player_id
        ):
            raise ValueError("Anchor must name an existing identity, player and team")
        if found[anchor.identity].conflicted:
            raise ValueError("An anchor identity contains simultaneous distinct bodies")
        if anchor.identity in by_id and by_id[anchor.identity] != anchor:
            raise ValueError("Conflicting anchors on one identity")
        by_id[anchor.identity] = anchor
    # Only anchors of one player can conflict: compare within each player, so a
    # whole match's thousands of number anchors stay linear in the players.
    by_player: dict[str, list[Anchor]] = defaultdict(list)
    for anchor in by_id.values():
        by_player[anchor.player_id].append(anchor)
    for members in by_player.values():
        for a, b in combinations(members, 2):
            if not compatible(found[a.identity], found[b.identity], by_id):
                raise ValueError(
                    "Verified player anchors conflict in team, number or live time"
                )
    return by_id


def pair_bounds(
    fragments: list[Fragment],
    anchors: dict[str, Anchor],
    probabilities: dict[tuple[int, int], float],
    pairs: list[tuple[int, int]],
    threshold: float,
) -> tuple[object, object, object]:
    """Construct hard and evidence bounds for each global pair variable."""
    np = importlib.import_module("numpy")
    lower, upper = np.zeros(len(pairs)), np.zeros(len(pairs))
    costs = np.zeros(len(pairs))
    for slot, (a, b) in enumerate(pairs):
        first, second = fragments[a], fragments[b]
        aa, ab = anchors.get(first.identity), anchors.get(second.identity)
        must = bool(aa and ab and aa.player_id == ab.player_id)
        allowed = compatible(first, second, anchors) and (
            must
            or (
                (aa.team if aa else first.team) in TEAMS
                and (ab.team if ab else second.team) in TEAMS
            )
        )
        probability = probabilities[a, b]
        upper[slot] = float(allowed and (must or probability >= threshold))
        lower[slot] = float(must)
        costs[slot] = -max(0.001, probability - threshold + 0.001)
    return lower, upper, costs


def adjacency(joined: set[tuple[int, int]]) -> dict[int, set[int]]:
    """List neighbours in the proposed partition."""
    neighbours: dict[int, set[int]] = defaultdict(set)
    for a, b in joined:
        neighbours[a].add(b)
        neighbours[b].add(a)
    return neighbours


def triangles(
    neighbours: dict[int, set[int]],
    joined: set[tuple[int, int]],
    slots: dict[tuple[int, int], int],
) -> set[tuple[int, int, int]]:
    """Find transitive violations, including missing/forbidden end-to-end edges."""
    added = set()
    for middle, ends in neighbours.items():
        for a, b in combinations(sorted(ends), 2):
            if (a, b) not in joined:
                positive = slots[min(a, middle), max(a, middle)]
                other = slots[min(b, middle), max(b, middle)]
                added.add((min(positive, other), max(positive, other), slots[a, b]))
    return added


def clique_groups(count: int, neighbours: dict[int, set[int]]) -> list[list[int]]:
    """Collect a proven transitive relation into disjoint complete clusters."""
    groups = []
    remaining = set(range(count))
    while remaining:
        seed = min(remaining)
        members = [seed, *sorted(neighbours.get(seed, set()))]
        groups.append(members)
        remaining.difference_update(members)
    return groups


@dataclass(frozen=True)
class Settings:
    """Recording namespace, competition-specific active bound and acceptance gate."""

    namespace: str = "match"
    roster_size: int = 8
    threshold: float = MIN_CONFIDENCE
    section_id: str = ""


def partition(
    fragments: list[Fragment],
    anchors: dict[str, Anchor],
    probabilities: dict[tuple[int, int], float],
    *,
    threshold: float,
    stopped: Callable[[], bool] | None = None,
) -> tuple[list[list[int]], str]:
    """Solve a global maximum-weight clique partition with HiGHS.

    All pair variables exist, including forbidden edges fixed to zero. Lazy
    triangle constraints enforce transitivity, so an A-B-C chain cannot bypass
    A-C simultaneity or verified conflicts. A timeout publishes no speculative
    edges. Human must-links remain subject to the same complete-cluster checks.
    """
    if stopped and stopped():
        return [], "interrupted"
    np = importlib.import_module("numpy")
    optimize = importlib.import_module("scipy.optimize")
    sparse = importlib.import_module("scipy.sparse")
    pairs = list(combinations(range(len(fragments)), 2))
    if not pairs:
        return [[i] for i in range(len(fragments))], "optimal"
    slots = {pair: i for i, pair in enumerate(pairs)}
    lower, upper = np.zeros(len(pairs)), np.zeros(len(pairs))
    costs = np.zeros(len(pairs))
    lower, upper, costs = pair_bounds(
        fragments, anchors, probabilities, pairs, threshold
    )
    constraints: set[tuple[int, int, int]] = set()
    # Bounds prevent unmeasured transitive merges. Every accepted cluster is a
    # clique of independently acceptable edges, or direct verified must-links.
    deadline = time.monotonic() + SOLVER_SECONDS
    for _ in range(MAX_SOLVER_ROUNDS):
        if stopped and stopped():
            return [], "interrupted"
        entries, columns, values = [], [], []
        for row, (positive, other, negative) in enumerate(sorted(constraints)):
            entries.extend([row] * 3)
            columns.extend([positive, other, negative])
            values.extend([1.0, 1.0, -1.0])
        matrix = sparse.coo_matrix(
            (values, (entries, columns)), shape=(len(constraints), len(pairs))
        ).tocsr()
        result = optimize.milp(
            costs,
            integrality=np.ones(len(pairs)),
            bounds=optimize.Bounds(lower, upper),
            constraints=optimize.LinearConstraint(matrix, -np.inf, 1.0),
            options={
                "time_limit": max(0.01, deadline - time.monotonic()),
                "mip_rel_gap": 0.0,
            },
        )
        if result.status != 0 or result.x is None:
            return anchored_partition(fragments, anchors), "unresolved_solver"
        joined = {
            pair
            for pair, value in zip(pairs, result.x, strict=True)
            if value > BINARY_SELECTED
        }
        neighbours = adjacency(joined)
        added = triangles(neighbours, joined, slots)
        if not added:
            return clique_groups(len(fragments), neighbours), "optimal"
        constraints.update(added)
        if time.monotonic() >= deadline:
            break
    return anchored_partition(fragments, anchors), "unresolved_solver"


def anchored_partition(
    fragments: list[Fragment], anchors: dict[str, Anchor]
) -> list[list[int]]:
    """Retain verified must-links only when automatic optimization is unfinished."""
    groups: dict[str, list[int]] = defaultdict(list)
    for index, piece in enumerate(fragments):
        anchor = anchors.get(piece.identity)
        groups[f"anchor:{anchor.player_id}" if anchor else f"unknown:{index}"].append(
            index
        )
    return list(groups.values())


def capacity_conflicts(
    fragments: list[Fragment],
    groups: list[list[int]],
    verified: dict[str, Anchor],
    roster_size: int,
) -> set[int]:
    """Reject overfull accepted lineups; never force unknown pieces into a roster."""
    occupied: dict[tuple[float, str], set[int]] = defaultdict(set)
    for group, members in enumerate(groups):
        if len(members) == 1 and fragments[members[0]].identity not in verified:
            continue
        for member in members:
            piece = fragments[member]
            anchor = verified.get(piece.identity)
            team = anchor.team if anchor else piece.team
            if team in TEAMS and not piece.replay:
                for timestamp in piece.times:
                    occupied[timestamp, team].add(group)
    return {
        group
        for members in occupied.values()
        if len(members) > roster_size
        for group in members
    }


DEFAULT_SETTINGS = Settings()


def associate(
    fragments: list[Fragment],
    *,
    anchors: list[Anchor] | None = None,
    calibration: Calibration | None = None,
    settings: Settings = DEFAULT_SETTINGS,
    stopped: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Assign shot fragments globally, preserving unknowns and calibrated confidence.

    Raises:
        ValueError: Inputs contain conflicting anchors or invalid configuration.

    """
    threshold, roster_size, namespace = (
        settings.threshold,
        settings.roster_size,
        settings.namespace,
    )
    if not 0.0 < threshold <= 1.0 or roster_size < 1:
        raise ValueError("Invalid confidence threshold or active roster size")
    if len({f.identity for f in fragments}) != len(fragments):
        raise ValueError("Shot identity IDs must be unique")
    verified = validate_anchors(fragments, anchors or [])
    eligible = [
        i
        for i, piece in enumerate(fragments)
        if piece.identity in verified
        or (calibration and piece.vectors)
        or any(n.calibrated for n in piece.numbers)
    ]
    if len(eligible) > MAX_FRAGMENTS:
        return {"version": VERSION, "status": "capacity_exceeded", "assignments": []}
    probabilities = {
        (a, b): evidence(fragments[a], fragments[b], calibration)
        for a, b in combinations(eligible, 2)
    }
    local_probabilities = {
        (a, b): probabilities[eligible[a], eligible[b]]
        for a, b in combinations(range(len(eligible)), 2)
    }
    working = [fragments[i] for i in eligible]
    local_groups, status = partition(
        working, verified, local_probabilities, threshold=threshold, stopped=stopped
    )
    groups = [[eligible[i] for i in group] for group in local_groups]
    included = set(eligible)
    groups.extend([i] for i in range(len(fragments)) if i not in included)
    if status == "interrupted":
        return {"version": VERSION, "status": status, "assignments": []}
    overloaded = capacity_conflicts(fragments, groups, verified, roster_size)
    assignments = []
    for group, members in enumerate(groups):
        named = [
            verified[fragments[i].identity]
            for i in members
            if fragments[i].identity in verified
        ]
        root = min(fragments[i].identity for i in members)
        birth = f"{settings.section_id}:{root}" if settings.section_id else root
        player_id = f"{namespace}:player:{named[0].player_id if named else birth}"
        scores = [
            probabilities[min(a, b), max(a, b)]
            for a, b in combinations(members, 2)
            if not (
                fragments[a].identity in verified and fragments[b].identity in verified
            )
        ]
        confidence = (
            max(
                0.0,
                1.0
                - sum(1.0 - score for score in scores)
                - sum(1.0 - a.confidence for a in named),
            )
            if scores or named
            else 0.0
        )
        accepted = (
            bool(named or len(members) > 1)
            and confidence >= threshold - 1e-9
            and group not in overloaded
        )
        for i in members:
            piece = fragments[i]
            direct = piece.identity in verified
            # Reject uncertain propagation without erasing the user's verified
            # names. Complete-cluster simultaneity and capacity still apply.
            admitted = accepted or (direct and group not in overloaded)
            assignments.append({
                "identity": piece.identity,
                "player_id": player_id if admitted else None,
                "display_id": None
                if not admitted
                else named[0].display_id
                if named and named[0].display_id
                else f"#{named[0].number}"
                if named and named[0].number
                else named[0].player_id
                if named
                else f"P{group + 1}",
                "status": "anchored"
                if direct and admitted
                else "assigned"
                if accepted
                else "unknown",
                "confidence": verified[piece.identity].confidence
                if direct and admitted
                else confidence
                if accepted
                else None,
                "source": verified[piece.identity].source
                if direct
                else "calibrated_evidence"
                if accepted
                else "unresolved",
            })
    return {
        "version": VERSION,
        "status": "completed",
        "solver": status,
        "calibration": calibration.provenance if calibration else None,
        "threshold": threshold,
        "roster_size": roster_size,
        "fragments": len(fragments),
        "solver_fragments": len(eligible),
        "confidence_kind": "union_bound",
        "joins": accepted_joins(assignments),
        "capacity_conflicts": len(overloaded),
        "assignments": assignments,
        "review_only": True,
    }


def accepted_joins(assignments: list[dict]) -> int:
    """Count only published aliases, rather than rejected solver proposals."""
    counts: dict[str, int] = defaultdict(int)
    for row in assignments:
        if row["player_id"]:
            counts[row["player_id"]] += 1
    return sum(count - 1 for count in counts.values())


def compose(report: dict, result: dict) -> dict:
    """Flatten match aliases into existing replay links, preserving frame overrides."""
    if result.get("status") != "completed":
        return {**report, "match_identity": result}
    assignments = {a["identity"]: a for a in result["assignments"] if a["player_id"]}

    def target(link: dict) -> dict:
        assignment = assignments.get(link["to_track_id"])
        if not assignment or link.get("label") == "referee":
            return dict(link)
        return {
            **link,
            "to_track_id": assignment["player_id"],
            "display_id": assignment["display_id"] or link.get("display_id"),
        }

    links = [target(link) for link in report.get("links", [])]
    source_ids = {link["from_track_id"] for link in links}
    links.extend(
        target({
            "from_track_id": identity,
            "to_track_id": identity,
            "source": "match_identity",
        })
        for identity in assignments
        if identity not in source_ids
    )
    return {
        **report,
        "links": links,
        "frame_links": [target(link) for link in report.get("frame_links", [])],
        "match_identity": result,
    }


def scope_link(link: dict, section: int, match_identity: dict | None = None) -> dict:
    """Scope raw section IDs while preserving proven recording-wide player targets."""
    stable = {
        a["player_id"]
        for a in (match_identity or {}).get("assignments", [])
        if a.get("player_id")
    }
    return {
        **link,
        "from_track_id": f"p{section}-{link['from_track_id']}",
        "to_track_id": link["to_track_id"]
        if link["to_track_id"] in stable
        else f"p{section}-{link['to_track_id']}",
        "processing_section": section,
        **{
            key: f"p{section}-{link[key]}"
            for key in ("superseded_track_id", "unnamed_track_id")
            if link.get(key)
        },
    }


def nearest_gap(first: set[float], second: set[float]) -> float:
    """Find the closest observed times without constructing a Cartesian product."""
    if not first or not second:
        return math.inf
    left, right = sorted(first), sorted(second)
    i, j, gap = 0, 0, math.inf
    while i < len(left) and j < len(right):
        gap = min(gap, abs(left[i] - right[j]))
        if left[i] < right[j]:
            i += 1
        else:
            j += 1
    return gap
