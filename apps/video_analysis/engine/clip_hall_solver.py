"""Joint calibration of every sampled view of each fixed broadcast camera.

Per camera group: rotation-only bundle adjustment of all keyframes; the hall's
three perpendicular line directions (floor markings, wall edges) with people
deciding which is down; both korf baskets as known 3D points; and standing
players as a scale check. A group that never shows both korfs stays
uncalibrated rather than guessing scale.
"""

from __future__ import annotations

import importlib
from itertools import pairwise
import math
from typing import TYPE_CHECKING, Any

from .clip_hall_court import (
    MIN_BASKET_CONFIDENCE,
    MIN_BASKET_VIEWS,
    MIN_FLOOR_SEGMENTS,
    CourtPlacement,
    hall_axes,
    segment_normal,
)
from .clip_hall_geometry import (
    EPSILON,
    MAX_ERROR_PIXELS,
    MAX_FOCAL,
    MIN_FOCAL,
    OVERLAY_COLUMNS,
    OVERLAY_ROWS,
    WIDTH,
    axis_angle,
    cell,
    correspond,
    features,
    orthonormal,
    ratio_matches,
    rays,
    rotation_focal,
    rotational,
    rotations,
    segments,
    spread,
    without,
)
from .clip_signals import modules


if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import NDArray

TEMPORAL_NEIGHBOURS = 4
MAX_ANCHOR_KEYFRAMES = 4
EDGE_POINTS = 30
MAX_KEYFRAMES = 240
MIN_GROUP_KEYFRAMES = 3
STATIONARY_PIXELS = 0.75
MOVING_PIXELS = 4.0
MOVING_SHARE = 0.5
MIN_OVERLAY_HITS = 3
MAX_EDGE_PIXELS = 8.0
COARSE_PIXELS = 25.0
STRUCTURE_TOLERANCES = (4.0, 1.5)
STRUCTURE_SIGMA = math.sin(math.radians(0.25))
MAX_BRIDGE_SECONDS = 3.0
LINK_SECONDS = 3.5
LINK_STRIDE = 3
LOOP_SAMPLES = 6
MAX_ADJUST_EVALUATIONS = 60


class Solver:
    """Jointly calibrate the sampled views of each fixed camera.

    Per group: rotation-only adjustment of every keyframe, the vertical direction
    from hall lines, then both korf baskets as known 3D points. A group that never
    shows both baskets stays uncalibrated rather than guessing scale.
    """

    def __init__(self, court: dict) -> None:
        """Collect keyframes before one bounded solve."""
        self.court = court
        self.keyframes: list[dict] = []
        self.edges: list[dict] = []
        self.groups: list[dict] = []
        self.diagnostics: list[dict] = []
        self.overlay: NDArray[Any] | None = None

    def add_keyframe(
        self, image: NDArray[Any], timestamp: float, objects: list
    ) -> None:
        """Retain one sampled view with its basket and vertical-line measurements."""
        if len(self.keyframes) >= MAX_KEYFRAMES:
            return
        people = [o["bbox"] for o in objects if o["label"] in {"player", "referee"}]
        found = features(image, [o["bbox"] for o in objects])
        if found["descriptors"] is None:
            return
        aspect = found["aspect"]
        detected = [
            o
            for o in objects
            if o["label"] == "basket"
            and o.get("confidence", 0) >= MIN_BASKET_CONFIDENCE
        ]
        baskets = [
            [
                o["bbox"][0] + o["bbox"][2] / 2 - 0.5,
                (o["bbox"][1] + o["bbox"][3] / 2 - 0.5) * aspect,
            ]
            for o in detected
        ]
        # A pose-trained detector also gives where each korf's pole meets the floor.
        feet = [
            [o["post_foot"][0] - 0.5, (o["post_foot"][1] - 0.5) * aspect]
            if isinstance(o.get("post_foot"), list)
            else None
            for o in detected
        ]
        self.keyframes.append({
            "time": timestamp,
            "features": found,
            "baskets": baskets,
            "feet": feet,
            "people": [
                [x - 0.5, (y - 0.5) * aspect, w, h * aspect] for x, y, w, h in people
            ],
            "segments": segments(image, people, aspect),
        })

    def order(self) -> list[int]:
        """Keyframe indices by time; context samples are appended after the clip."""
        return sorted(
            range(len(self.keyframes)), key=lambda i: self.keyframes[i]["time"]
        )

    def gaps(self) -> list[tuple[float, float]]:
        """Adjacent samples that do not match: a pan may have happened between them."""
        order = self.order()
        return [
            (self.keyframes[i]["time"], self.keyframes[j]["time"])
            for i, j in pairwise(order)
            if self.keyframes[j]["time"] - self.keyframes[i]["time"]
            <= MAX_BRIDGE_SECONDS
            and correspond(self.keyframes[i]["features"], self.keyframes[j]["features"])
            is None
        ]

    def link(self, i: int, j: int) -> bool:
        """Store one accepted match with a bounded, deterministic point sample."""
        _, np = modules()
        match = correspond(self.keyframes[i]["features"], self.keyframes[j]["features"])
        if match is None or not rotational(*match):
            return False
        p, q, homography = match
        chosen = (
            np.linspace(0, len(p) - 1, EDGE_POINTS).round().astype(int)
            if len(p) > EDGE_POINTS
            else np.arange(len(p))
        )
        self.edges.append({
            "a": i,
            "b": j,
            "p": p[chosen],
            "q": q[chosen],
            "homography": homography,
            "inliers": len(p),
        })
        return True

    def learn_overlay(self) -> None:
        """Find broadcast graphics: features that stay put while the camera moves."""
        _, np = modules()
        order = self.order()
        hits = np.zeros(OVERLAY_ROWS * OVERLAY_COLUMNS, dtype=int)
        for i, j in pairwise(order):
            a, b = self.keyframes[i]["features"], self.keyframes[j]["features"]
            matched = ratio_matches(a, b)
            if matched is None:
                continue
            moved = np.linalg.norm(matched[0] - matched[1], axis=1) * WIDTH
            still = moved < STATIONARY_PIXELS
            # Only a clearly moving view separates graphics from a still camera.
            if (moved > MOVING_PIXELS).mean() < MOVING_SHARE or not still.any():
                continue
            hits[np.unique(cell(matched[0][still], a["aspect"]))] += 1
        grid = (hits >= MIN_OVERLAY_HITS).reshape(OVERLAY_ROWS, OVERLAY_COLUMNS)
        # Graphics are boxes; include their immediate neighbours.
        grown = grid.copy()
        grown[1:] |= grid[:-1]
        grown[:-1] |= grid[1:]
        grown[:, 1:] |= grid[:, :-1]
        grown[:, :-1] |= grid[:, 1:]
        self.overlay = grown.ravel()
        for keyframe in self.keyframes:
            keyframe["features"] = without(keyframe["features"], self.overlay)

    def connect(self, stopped: Callable[[], bool]) -> None:
        """Match time neighbours, then close loops between the resulting groups."""
        self.learn_overlay()
        order = self.order()
        times = [self.keyframes[i]["time"] for i in order]
        # A cut can hide a pan: compare every pair of views a few seconds apart.
        pairs = {
            (order[n], order[m])
            for n in range(len(order))
            for m in range(n + 1, len(order))
            if m - n <= TEMPORAL_NEIGHBOURS
            or (times[m] - times[n] <= LINK_SECONDS and (m - n) % LINK_STRIDE == 0)
        }
        # Views showing a basket anchor the court; join them across the clip.
        anchors = [i for i in order if self.keyframes[i]["baskets"]]
        step = max(1, math.ceil(len(anchors) / MAX_ANCHOR_KEYFRAMES))
        pairs.update(
            (a, b)
            for a in anchors[::step]
            for b in anchors
            if a != b and (b, a) not in pairs
        )
        for i, j in sorted(pairs):
            if stopped():
                return
            self.link(i, j)
        # The same camera can return after a cut; try a few views of each group.
        groups = [
            g for g in self.components(self.edges) if len(g) >= MIN_GROUP_KEYFRAMES
        ]
        for n, first in enumerate(groups):
            for second in groups[n + 1 :]:
                if stopped():
                    return
                sample = (
                    [first[round(t)] for t in spread(len(first), LOOP_SAMPLES)],
                    [second[round(t)] for t in spread(len(second), LOOP_SAMPLES)],
                )
                for i in sample[0]:
                    for j in sample[1]:
                        self.link(i, j)

    def components(self, edges: list[dict]) -> list[list[int]]:
        """Group keyframes connected by accepted matches."""
        parent = list(range(len(self.keyframes)))

        def root(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for edge in edges:
            parent[root(edge["a"])] = root(edge["b"])
        groups: dict[int, list[int]] = {}
        for i in range(len(self.keyframes)):
            groups.setdefault(root(i), []).append(i)
        return sorted(groups.values(), key=len, reverse=True)

    def chain(self, members: list[int], edges: list[dict], focal: float) -> dict | None:
        """Initialize relative rotations along a maximum-support spanning tree."""
        _, np = modules()
        rotation = {members[0]: np.eye(3)}
        focals = {members[0]: focal}
        adjacency: dict[int, list] = {}
        for edge in edges:
            adjacency.setdefault(edge["a"], []).append((edge, False))
            adjacency.setdefault(edge["b"], []).append((edge, True))
        frontier = list(adjacency.get(members[0], []))
        while frontier:
            frontier.sort(key=lambda item: item[0]["inliers"])
            edge, reverse = frontier.pop()
            source, target = (
                (edge["b"], edge["a"]) if reverse else (edge["a"], edge["b"])
            )
            if target in rotation or source not in rotation:
                continue
            homography = (
                np.linalg.inv(edge["homography"]) if reverse else edge["homography"]
            )
            # A shared focal keeps chained errors from compounding; the bundle
            # adjustment frees each view's zoom afterwards.
            k = np.diag([focal, focal, 1.0])
            step = orthonormal(np.linalg.inv(k) @ homography @ k)
            rotation[target] = step @ rotation[source]
            focals[target] = focal
            frontier.extend(adjacency.get(target, []))
        # Views reachable only through non-rotational matches are left out.
        if len(rotation) < MIN_GROUP_KEYFRAMES:
            return None
        return {"rotation": rotation, "focal": focals}

    def adjust(
        self,
        members: list[int],
        edges: list[dict],
        state: dict,
        scale: float = 1.0,
        structure: dict | None = None,
    ) -> tuple[dict, float]:
        """Bundle-adjust keyframe rotations and focals from pairwise matches.

        The first keyframe fixes the rotation gauge; the court is placed later.
        With a hall structure, every assigned line segment must also run along
        one of three shared perpendicular directions. Wall matches alone cannot
        see a small zoom or roll error that tilts the floor between views.
        """
        _, np = modules()
        optimize = importlib.import_module("scipy.optimize")
        sparse = importlib.import_module("scipy.sparse")
        index = {k: n for n, k in enumerate(members)}
        views = 4 * len(members)
        size = views + (3 if structure else 0)
        pair_a = np.concatenate([np.full(len(e["p"]), index[e["a"]]) for e in edges])
        pair_b = np.concatenate([np.full(len(e["p"]), index[e["b"]]) for e in edges])
        pair_p = np.concatenate([e["p"] for e in edges])
        pair_q = np.concatenate([e["q"] for e in edges])
        lines = structure["segments"] if structure else []
        hall = structure["axes"] if structure else np.eye(3)
        line_k = np.array([index[k] for k, _, _ in lines], dtype=int)
        line_axis = np.array([axis for _, _, axis in lines], dtype=int)
        ends = np.array([segment for _, segment, _ in lines]).reshape(-1, 2, 2)
        rows = 2 * len(pair_p) + 3 + len(lines)
        sparsity = sparse.lil_matrix((rows, size), dtype=int)
        for row, (a, b) in enumerate(zip(pair_a, pair_b, strict=True)):
            for column in (*range(4 * a, 4 * a + 4), *range(4 * b, 4 * b + 4)):
                sparsity[2 * row, column] = sparsity[2 * row + 1, column] = 1
        root = index[members[0]]
        for axis in range(3):
            sparsity[2 * len(pair_p) + axis, 4 * root + axis] = 1
        for row, k in enumerate(line_k, start=2 * len(pair_p) + 3):
            for column in (*range(4 * k, 4 * k + 4), *range(views, size)):
                sparsity[row, column] = 1

        def unpack(x: NDArray[Any]) -> tuple:
            blocks = x[:views].reshape(-1, 4)
            return rotations(blocks[:, :3]), np.exp(blocks[:, 3])

        def residual(x: NDArray[Any]) -> NDArray[Any]:
            rotation, focal = unpack(x)
            directions = np.einsum(
                "ni,nij->nj",
                np.c_[pair_p / focal[pair_a, None], np.ones(len(pair_p))],
                rotation[pair_a],
            )
            camera = np.einsum("nij,nj->ni", rotation[pair_b], directions)
            depth = np.maximum(camera[:, 2], EPSILON)
            image = focal[pair_b, None] * camera[:, :2] / depth[:, None]
            parts = [
                ((image - pair_q) * WIDTH).ravel(),
                x[4 * root : 4 * root + 3] * 1e4,
            ]
            if len(lines):
                axes = hall @ rotations(x[views:])[0].T
                normals = np.cross(
                    np.einsum(
                        "ni,nij->nj",
                        np.c_[ends[:, 0] / focal[line_k, None], np.ones(len(ends))],
                        rotation[line_k],
                    ),
                    np.einsum(
                        "ni,nij->nj",
                        np.c_[ends[:, 1] / focal[line_k, None], np.ones(len(ends))],
                        rotation[line_k],
                    ),
                )
                normals /= np.linalg.norm(normals, axis=1, keepdims=True)
                parts.append(
                    np.einsum("ni,ni->n", normals, axes[line_axis]) / STRUCTURE_SIGMA
                )
            return np.concatenate(parts)

        start = np.concatenate([
            [*axis_angle(state["rotation"][k]), math.log(state["focal"][k])]
            for k in members
        ])
        start = np.r_[start, np.zeros(size - views)]
        lower, upper = np.full(size, -np.inf), np.full(size, np.inf)
        lower[3:views:4], upper[3:views:4] = math.log(MIN_FOCAL), math.log(MAX_FOCAL)
        result = optimize.least_squares(
            residual,
            np.clip(start, lower + EPSILON, upper - EPSILON),
            bounds=(lower, upper),
            jac_sparsity=sparsity,
            loss="soft_l1",
            f_scale=scale,
            x_scale="jac",
            max_nfev=MAX_ADJUST_EVALUATIONS,
            tr_solver="lsmr",
        )
        rotation, focal = unpack(result.x)
        solved = {
            "rotation": {k: rotation[index[k]] for k in members},
            "focal": {k: float(focal[index[k]]) for k in members},
        }
        return solved, float(np.median(np.abs(residual(result.x)[: 2 * len(pair_p)])))

    def structure(
        self, members: list[int], state: dict, tolerance: float
    ) -> dict | None:
        """Assign every view's line segments to the hall's three directions."""
        _, np = modules()
        owners = [
            (k, segment) for k in members for segment in self.keyframes[k]["segments"]
        ]
        if len(owners) < MIN_FLOOR_SEGMENTS:
            return None
        normals = np.array([
            segment_normal(segment, state["rotation"][k], state["focal"][k])
            for k, segment in owners
        ])
        axes = hall_axes(normals)
        if axes is None:
            return None
        alignment = np.abs(normals @ axes.T)
        nearest = alignment.argmin(axis=1)
        keep = alignment.min(axis=1) < math.sin(math.radians(tolerance))
        return {
            "axes": axes,
            "segments": [
                (k, segment, int(axis))
                for (k, segment), axis, kept in zip(owners, nearest, keep, strict=True)
                if kept
            ],
        }

    def edge_errors(self, edges: list[dict], state: dict) -> list[float]:
        """Median reprojection error of each pairwise match, in WIDTH pixels."""
        _, np = modules()
        errors = []
        for edge in edges:
            a, b = edge["a"], edge["b"]
            camera = (
                rays(edge["p"], state["rotation"][a], state["focal"][a])
                @ state["rotation"][b].T
            )
            if (camera[:, 2] <= 0).any():
                errors.append(math.inf)
                continue
            image = state["focal"][b] * camera[:, :2] / camera[:, 2:]
            errors.append(
                float(np.median(np.linalg.norm(image - edge["q"], axis=1)) * WIDTH)
            )
        return errors

    def prune(self, members: list[int], edges: list[dict], state: dict) -> tuple:
        """Drop matches a single rotating camera cannot explain, keep the main part."""
        errors = self.edge_errors(edges, state)
        kept = [
            e
            for e, error in zip(edges, errors, strict=True)
            if error <= MAX_EDGE_PIXELS
        ]
        bounded = {
            k
            for k in members
            if not MIN_FOCAL * 1.01 < state["focal"][k] < MAX_FOCAL * 0.99
        }
        kept = [e for e in kept if e["a"] not in bounded and e["b"] not in bounded]
        member_set = set(members) - bounded
        parts = [
            [k for k in part if k in member_set]
            for part in self.components(kept)
            if any(k in member_set for k in part)
        ]
        best = max(
            parts,
            key=lambda part: (
                sum(bool(self.keyframes[k]["baskets"]) for k in part),
                len(part),
            ),
            default=[],
        )
        best_set = set(best)
        return best, [e for e in kept if e["a"] in best_set and e["b"] in best_set]

    def rotations_of(self, members: list[int], edges: list[dict]) -> tuple | None:
        """Adjust the relative rotations of one camera group, without the court."""
        state = self.chain(members, edges, rotation_focal(edges))
        if state is None:
            self.diagnostics.append({
                "members": len(members),
                "failure": "no_rotation_chain",
            })
            return None
        members = [k for k in members if k in state["rotation"]]
        reached = set(members)
        edges = [e for e in edges if e["a"] in reached and e["b"] in reached]
        # Coarse-to-fine: a wide robust scale first lets thin links between parts
        # of the pan align whole blocks of views before small errors dominate.
        state, _ = self.adjust(members, edges, state, COARSE_PIXELS)
        state, error = self.adjust(members, edges, state)
        pruned, kept = self.prune(members, edges, state)
        if len(pruned) >= MIN_GROUP_KEYFRAMES and len(edges) > len(kept) > 0:
            members, edges = pruned, kept
            state, error = self.adjust(members, edges, state)
        if len(pruned) >= MIN_GROUP_KEYFRAMES and kept:
            # Tie every view to the hall's line directions, first loosely.
            for tolerance in STRUCTURE_TOLERANCES:
                structure = self.structure(members, state, tolerance)
                if structure is None:
                    break
                state, error = self.adjust(members, edges, state, structure=structure)
        if len(pruned) < MIN_GROUP_KEYFRAMES or not kept or error > MAX_ERROR_PIXELS:
            self.diagnostics.append({
                "members": len(members),
                "failure": "rotation_model",
                "error": round(error, 3),
            })
            return None
        return members, state, error

    def solve_group(self, members: list[int], edges: list[dict]) -> dict | None:
        """Calibrate one camera; abstain without both baskets and a vertical."""
        found = self.rotations_of(members, edges) if edges else None
        if found is None:
            return None
        members, state, error = found
        court = CourtPlacement(self.keyframes, self.court, self.diagnostics)
        fitted = court.fit(members, state)
        if fitted is None:
            return None
        solved, errors = fitted
        return {
            "members": members,
            "state": solved,
            "reference_time": min(self.keyframes[k]["time"] for k in members),
            "observations": len(errors),
            "observation_error_pixels": round(
                float(sorted(errors)[len(errors) // 2]), 2
            ),
            "rotation_error_pixels": round(error, 3),
        }

    def solve(self, stopped: Callable[[], bool] | None = None) -> list[dict]:
        """Build camera groups and calibrate each one that shows both korfs."""
        stopped = stopped or (lambda: False)
        self.connect(stopped)
        groups = []
        for members in self.components(self.edges):
            if stopped() or len(members) < MIN_GROUP_KEYFRAMES:
                continue
            # Placing the court needs both korfs; skip views that cannot show them.
            if (
                sum(bool(self.keyframes[k]["baskets"]) for k in members)
                < 2 * MIN_BASKET_VIEWS
            ):
                self.diagnostics.append({
                    "members": len(members),
                    "failure": "fewer_than_two_baskets",
                })
                continue
            member_set = set(members)
            edges = [e for e in self.edges if e["a"] in member_set]
            try:
                group = self.solve_group(members, edges)
            except (ValueError, ArithmeticError, IndexError):
                group = None
            if group is not None:
                groups.append(group)
        self.groups = groups
        return groups
