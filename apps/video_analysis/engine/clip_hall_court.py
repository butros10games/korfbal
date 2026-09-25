"""Place the court in a fixed camera's rotation-adjusted views.

Relative keyframe rotations are known. The hall's three perpendicular line
directions give the vertical (people decide which is down), the two korf
baskets are known 3D points that fix camera height, court direction and
origin, floor-line families refine tilt and heading, and standing players
check the scale.
"""

from __future__ import annotations

import importlib
from itertools import pairwise
import math
from operator import itemgetter
from typing import TYPE_CHECKING, Any

from .clip_hall_geometry import (
    EPSILON,
    PLAYER_HEIGHT,
    WIDTH,
    heights,
    project,
    rays,
    rotations,
    standing,
)
from .clip_signals import modules


if TYPE_CHECKING:
    from numpy.typing import NDArray

MIN_HEIGHT, MAX_HEIGHT = 1.5, 40.0
BASKET_HEIGHT = 3.4
BASKET_OFFSET = 0.24
FOOT_SIGMA_PIXELS = 2.0
MIN_LINE_PIECE = 0.5
LINE_WINDOWS = ((8.0, 2.0), (4.0, 1.2), (2.0, 0.6))
LINE_WINDOW = 1.2
LINE_PRIOR = 0.6
LINE_BASKET_RATIO = 2.0
LINE_BASKET_PIXELS = 8.0
LINE_BIN = 0.15
MIN_LINE_METRES = 8.0
MIN_LINE_VIEWS = 3
LINE_SPACING_TOLERANCE = 0.8
LINE_SIGMA_PIXELS = 1.0
LINE_CLIP_PIXELS = 30.0
MIN_BASKET_CONFIDENCE = 0.5
MIN_BASKET_VIEWS = 3
BASKET_SIGMA_PIXELS = 8.0
MAX_BASKET_PIXELS = 40.0
CLUSTER_DEGREES = 2.5
MIN_POST_SEPARATION_DEGREES = 10.0
FLOOR_MARGIN = 3.0
MIN_FLOOR_SEGMENTS = 100
MIN_FAMILY_SEGMENTS = 30
FLOOR_TOLERANCE_DEGREES = 0.5
FLOOR_TRIALS = 3000
MAX_FAMILY_DOT = 0.03
MAX_FLOOR_TILT_DEGREES = 25.0
FLOOR_TILT_SIGMA = math.sin(math.radians(0.02))
FLOOR_HEADING_SIGMA = math.sin(math.radians(0.1))
PLAYER_SIGMA = 0.15
FLOOR_ROUNDS = 2
MIN_REFINE_SEGMENTS = 2
MIN_GAUGE_AXIS = 0.1


def segment_normal(segment: list, rotation: NDArray[Any], focal: float) -> NDArray[Any]:
    """Return the unit normal of the plane through the camera and a segment."""
    _, np = modules()
    first, second = rays(np.array(segment), rotation, focal)
    normal = np.cross(first, second)
    return normal / np.linalg.norm(normal)


def hall_axes(normals: NDArray[Any]) -> NDArray[Any] | None:
    """Three orthogonal directions shared by the most segment planes (RANSAC)."""
    _, np = modules()
    limit = math.sin(math.radians(FLOOR_TOLERANCE_DEGREES))
    generator = np.random.default_rng(0)
    best = None
    for _ in range(FLOOR_TRIALS):
        a, b, c = generator.choice(len(normals), 3, replace=False)
        first = np.cross(normals[a], normals[b])
        second = np.cross(normals[c], first)
        if min(np.linalg.norm(first), np.linalg.norm(second)) < EPSILON:
            continue
        first /= np.linalg.norm(first)
        second /= np.linalg.norm(second)
        axes = np.array([first, second, np.cross(first, second)])
        counts = (np.abs(normals @ axes.T) < limit).sum(axis=0)
        if best is None or counts.min() > best[0]:
            best = (int(counts.min()), axes)
    if best is None or best[0] < MIN_FAMILY_SEGMENTS:
        return None
    # Refine each direction from its own segments, then re-orthogonalize.
    support = np.abs(normals @ best[1].T) < limit
    refined = np.array([
        np.linalg.svd(normals[support[:, n]])[2][-1]
        if support[:, n].sum() >= MIN_REFINE_SEGMENTS
        else best[1][n]
        for n in range(3)
    ])
    u, _, vt = np.linalg.svd(refined)
    return u @ vt


def floor_axes(normals: NDArray[Any], rough_down: NDArray[Any]) -> tuple | None:
    """Two perpendicular floor-line directions near the rough floor (RANSAC)."""
    _, np = modules()
    limit = math.sin(math.radians(FLOOR_TOLERANCE_DEGREES))
    tilt = math.cos(math.radians(MAX_FLOOR_TILT_DEGREES))
    generator = np.random.default_rng(0)
    best = None
    for _ in range(FLOOR_TRIALS):
        a, b, c, d = generator.choice(len(normals), 4, replace=False)
        first = np.cross(normals[a], normals[b])
        second = np.cross(normals[c], normals[d])
        if min(np.linalg.norm(first), np.linalg.norm(second)) < EPSILON:
            continue
        first /= np.linalg.norm(first)
        second /= np.linalg.norm(second)
        up = np.cross(first, second)
        if abs(first @ second) > MAX_FAMILY_DOT or abs(
            up @ rough_down
        ) < tilt * np.linalg.norm(up):
            continue
        one, two = np.abs(normals @ first) < limit, np.abs(normals @ second) < limit
        if best is None or min(one.sum(), two.sum()) > best[0]:
            best = (min(one.sum(), two.sum()), one, two)
    if best is None or best[0] < MIN_FAMILY_SEGMENTS:
        return None
    masks = best[1:]
    return [np.linalg.svd(normals[m])[2][-1] for m in masks], masks


def court_line_positions(court: dict) -> list[tuple[int, float]]:
    """End lines and centre line (constant x) and sidelines (constant y)."""
    length, width = court["length"], court["width"]
    return [(0, 0.0), (0, length / 2), (0, length), (1, 0.0), (1, width)]


def strongest(bucket: dict) -> tuple | None:
    """Pick the painted line near the prediction with the most supporting length.

    Halls can paint parallel lines a metre apart. Length alone may pick the
    wrong one, so length is weighted by closeness to where the rest of the
    model (posts, heading, other lines) predicts the court line.
    """
    _, np = modules()

    def near(key: int) -> list:
        return [
            i for d in (-1, 0, 1) for i in bucket.get(key + d, {"items": []})["items"]
        ]

    def support(key: int) -> float:
        # Merge neighbouring bins so one painted line is not split in two.
        length = sum(bucket.get(key + d, {"length": 0})["length"] for d in (-1, 0, 1))
        return length * math.exp(-abs(key * LINE_BIN) / LINE_PRIOR)

    key = max(bucket, key=support)
    items = near(key)
    length = sum(bucket.get(key + d, {"length": 0})["length"] for d in (-1, 0, 1))
    if length < MIN_LINE_METRES or len({i[0] for i in items}) < MIN_LINE_VIEWS:
        return None
    return float(np.median([i[4] for i in items])), items


def consistent(chosen: dict) -> dict:
    """Drop an end or centre line whose spacing to its neighbour is off."""
    for axis in (0, 1):
        ends = sorted(v for a, v in chosen if a == axis)
        for first, second in pairwise(ends):
            if (axis, first) not in chosen or (axis, second) not in chosen:
                continue
            gap = second + chosen[axis, second][0] - first - chosen[axis, first][0]
            if abs(gap - (second - first)) > LINE_SPACING_TOLERANCE:
                # Keep the pair member closer to its prediction.
                worse = max((first, second), key=lambda v: abs(chosen[axis, v][0]))
                chosen.pop((axis, worse))
    return chosen


def rays_depth(segment: list, rotation: NDArray[Any], focal: float) -> list[float]:
    """Downward component of each unit viewing ray of a segment's ends."""
    _, np = modules()
    directions = rays(np.array(segment), rotation, focal)
    return (directions[:, 2] / np.linalg.norm(directions, axis=1)).tolist()


def pinned_feet(state: dict, placed: dict) -> tuple:
    """Rays, post positions and metres-per-pixel of detected pole feet.

    A pole foot on the floor is an exact post position, far better conditioned
    than a basket hanging barely below a low camera.
    """
    _, np = modules()
    footed = [d for d in placed["baskets"] if d.get("foot") is not None]
    directions = np.array([
        rays(
            np.array([d["foot"]]),
            state["rotation"][d["keyframe"]],
            state["focal"][d["keyframe"]],
        )[0]
        for d in footed
    ]).reshape(-1, 3)
    posts = np.array([d["post"] for d in footed]).reshape(-1, 2)
    scale = np.array([
        floor_scale(placed, state, d["keyframe"], d["foot"]) for d in footed
    ])
    return directions, posts, scale


def painted_lines(state: dict, placed: dict) -> tuple:
    """Rays, axes, positions and metres-per-pixel of snapped segment ends."""
    _, np = modules()
    lines = placed.get("lines", [])
    directions = np.array([
        rays(np.array(segment), state["rotation"][k], state["focal"][k])
        for k, segment, _, _ in lines
    ]).reshape(-1, 3)
    axis = np.repeat([a for _, _, a, _ in lines], 2).astype(int)
    value = np.repeat([v for _, _, _, v in lines], 2)
    # Far, grazing floor points get proportionally less weight.
    scale = np.array([
        floor_scale(placed, state, k, end)
        for k, segment, _, _ in lines
        for end in segment
    ])
    return directions, axis, value, scale


def on_floor(world_rays: NDArray[Any], centre: NDArray[Any]) -> NDArray[Any]:
    """Where rays from the camera centre meet the floor (z = 0)."""
    _, np = modules()
    reach = -centre[2] / np.maximum(world_rays[:, 2], EPSILON)
    return centre[None, :2] + reach[:, None] * world_rays[:, :2]


def floor_scale(placed: dict, state: dict, k: int, point: list) -> float:
    """Metres of floor per image pixel at one image point, along the view."""
    depth = rays_depth([point, point], placed["rotation"][k], state["focal"][k])[0]
    return -placed["centre"][2] / (state["focal"][k] * WIDTH * max(depth, EPSILON) ** 2)


class CourtPlacement:
    """Court placement for one camera group; failures are recorded, not raised."""

    def __init__(self, keyframes: list[dict], court: dict, diagnostics: list) -> None:
        """Share the solver's keyframes, court size and diagnostics."""
        self.keyframes = keyframes
        self.court = court
        self.diagnostics = diagnostics

    def orient(self, members: list[int], state: dict, placed: dict) -> dict:
        """Fit the world rotation and camera centre with relative poses held fixed.

        Baskets are known 3D points, floor lines lie in the floor and standing
        players have a typical height. Tens of thousands of pairwise
        residuals already fix the relative rotations; six shared parameters remain.
        """
        _, np = modules()
        optimize = importlib.import_module("scipy.optimize")
        k0 = members[0]
        # placed["rotation"][k] = relative[k] @ world
        world = state["rotation"][k0].T @ placed["rotation"][k0]
        relative = np.array([state["rotation"][k] for k in members])
        focal = np.array([state["focal"][k] for k in members])
        index = {k: n for n, k in enumerate(members)}
        basket_k = np.array(
            [index[d["keyframe"]] for d in placed["baskets"]], dtype=int
        )
        basket_world = np.array([d["world"] for d in placed["baskets"]])
        basket_image = np.array([d["image"] for d in placed["baskets"]])
        # Floor lines fix tilt: both families' directions lie in the floor. Once
        # every view agrees on them, they also fix heading: courts are painted
        # along the hall's line directions, so each family runs along one axis.
        families = placed.get("families", [])
        directions, crossing = [], []
        for axis in (0, 1):
            normals = [
                segment_normal(segment, state["rotation"][k], state["focal"][k])
                for k, segment, family in families
                if family == axis
            ]
            if len(normals) >= MIN_REFINE_SEGMENTS:
                directions.append(np.linalg.svd(np.array(normals))[2][-1])
                crossing.append(1 - axis)
        directions = np.array(directions).reshape(-1, 3)
        person_k, feet, heads = [], [], []
        for k in members:
            for foot, head in standing(self.keyframes[k].get("people", [])):
                person_k.append(index[k])
                feet.append(foot)
                heads.append(head)
        person_k = np.array(person_k, dtype=int)
        feet, heads = np.array(feet).reshape(-1, 2), np.array(heads).reshape(-1, 2)

        def poses(x: NDArray[Any]) -> tuple:
            turned = world @ rotations(x[:3])[0]
            return relative @ turned, turned

        foot_rays, foot_post, foot_scale = pinned_feet(state, placed)
        line_rays, line_axis, line_value, line_scale = painted_lines(state, placed)

        def residual(x: NDArray[Any]) -> NDArray[Any]:
            rotation, turned = poses(x)
            centre = x[3:]
            camera = np.einsum("nij,nj->ni", rotation[basket_k], basket_world - centre)
            depth = np.maximum(camera[:, 2], EPSILON)
            image = focal[basket_k, None] * camera[:, :2] / depth[:, None]
            parts = [((image - basket_image) * WIDTH / BASKET_SIGMA_PIXELS).ravel()]
            # Floor directions are in the reference view; world z there is turned[:, 2].
            parts.extend((
                directions @ turned[:, 2] / FLOOR_TILT_SIGMA,
                np.einsum("ni,in->n", directions, turned[:, crossing])
                / FLOOR_HEADING_SIGMA,
            ))
            if len(foot_rays):
                floor = on_floor(foot_rays @ turned, centre)
                offset = np.linalg.norm(floor - foot_post, axis=1) / foot_scale
                parts.append(np.minimum(offset, LINE_CLIP_PIXELS) / FOOT_SIGMA_PIXELS)
            if len(line_rays):
                # Painted court lines: each segment end lies on its line's position.
                floor = on_floor(line_rays @ turned, centre)
                offset = floor[np.arange(len(floor)), line_axis] - line_value
                pixels = offset / line_scale
                parts.append(
                    np.clip(pixels, -LINE_CLIP_PIXELS, LINE_CLIP_PIXELS)
                    / LINE_SIGMA_PIXELS
                )
            if len(person_k):
                parts.append(
                    (
                        heights(
                            rotation[person_k], focal[person_k], centre, feet, heads
                        )
                        - PLAYER_HEIGHT
                    )
                    / PLAYER_SIGMA
                )
            return np.concatenate(parts)

        result = optimize.least_squares(
            residual, np.r_[0, 0, 0, placed["centre"]], loss="soft_l1", f_scale=1.0
        )
        rotation, _ = poses(result.x)
        return {
            "rotation": {k: rotation[index[k]] for k in members},
            "focal": {k: float(focal[index[k]]) for k in members},
            "centre": result.x[3:],
        }

    def vertical(self, members: list[int], state: dict) -> NDArray[Any] | None:
        """Find the downward one of the hall's three perpendicular line directions.

        Floor markings, wall edges and columns run along three orthogonal
        directions. Fitting all three together keeps floor lines that point at
        the camera (near-vertical in the image) from being taken as vertical.
        People decide which direction is down: feet are below heads.
        """
        _, np = modules()
        normals = [
            segment_normal(segment, state["rotation"][k], state["focal"][k])
            for k in members
            for segment in self.keyframes[k]["segments"]
        ]
        axes = (
            hall_axes(np.array(normals)) if len(normals) >= MIN_FLOOR_SEGMENTS else None
        )
        if axes is None:
            return None
        drops = []
        for k in members:
            for foot, head in standing(self.keyframes[k].get("people", [])):
                f, h = rays(
                    np.array([foot, head]), state["rotation"][k], state["focal"][k]
                )
                drops.append(f / np.linalg.norm(f) - h / np.linalg.norm(h))
        if len(drops) < MIN_BASKET_VIEWS:
            return None
        lean = np.array(drops) @ axes.T
        index = int(np.argmax(np.abs(lean.mean(axis=0)) / (lean.std(axis=0) + EPSILON)))
        return axes[index] * np.sign(lean[:, index].mean())

    def posts(self, members: list[int], state: dict, down: NDArray[Any]) -> list | None:
        """Cluster basket rays by bearing into the two korfs of the court."""
        _, np = modules()
        detections: list[dict[str, Any]] = []
        axis = np.cross(down, [1.0, 0, 0])
        if np.linalg.norm(axis) < MIN_GAUGE_AXIS:
            axis = np.cross(down, [0, 0, 1.0])
        axis /= np.linalg.norm(axis)
        other = np.cross(down, axis)
        for k in members:
            if not self.keyframes[k]["baskets"]:
                continue
            directions = rays(
                np.array(self.keyframes[k]["baskets"]),
                state["rotation"][k],
                state["focal"][k],
            )
            feet = self.keyframes[k].get("feet") or [None] * len(directions)
            for image, direction, foot in zip(
                self.keyframes[k]["baskets"], directions, feet, strict=True
            ):
                unit = direction / np.linalg.norm(direction)
                detections.append({
                    "keyframe": k,
                    "image": image,
                    "foot": foot,
                    "ray": unit,
                    "bearing": math.degrees(math.atan2(unit @ other, unit @ axis)),
                })
        detections.sort(key=itemgetter("bearing"))
        clusters: list[list[dict]] = []
        for detection in detections:
            if (
                clusters
                and detection["bearing"] - clusters[-1][-1]["bearing"]
                <= CLUSTER_DEGREES
            ):
                clusters[-1].append(detection)
            else:
                clusters.append([detection])
        clusters = [
            c for c in clusters if len({d["keyframe"] for d in c}) >= MIN_BASKET_VIEWS
        ]
        clusters.sort(key=len, reverse=True)
        if len(clusters) < 2:  # noqa: PLR2004
            return None
        pair = clusters[:2]
        if (
            abs(
                np.median([d["bearing"] for d in pair[0]])
                - np.median([d["bearing"] for d in pair[1]])
            )
            < MIN_POST_SEPARATION_DEGREES
        ):
            return None
        return pair

    def place(
        self, members: list[int], state: dict, down: NDArray[Any] | None = None
    ) -> dict | None:
        """Fix the court from the downward direction and both basket rays."""
        _, np = modules()
        if down is None:
            down = self.vertical(members, state)
        if down is None:
            self.diagnostics.append({"members": len(members), "failure": "no_vertical"})
            return None
        pair = self.posts(members, state, down)
        if pair is None:
            self.diagnostics.append({
                "members": len(members),
                "failure": "fewer_than_two_baskets",
            })
            return None
        rays_ = []
        for cluster in pair:
            mean = np.mean([d["ray"] for d in cluster], axis=0)
            rays_.append(mean / np.linalg.norm(mean))
        length, width = self.court["length"], self.court["width"]
        # Each basket hangs in front of its pole, towards the court centre.
        separation = length * 2 / 3 - 2 * BASKET_OFFSET
        height = BASKET_HEIGHT
        for order in ((0, 1), (1, 0)):
            first, second = rays_[order[0]], rays_[order[1]]
            depth = [first @ down, second @ down]
            if min(abs(d) for d in depth) < EPSILON or depth[0] * depth[1] < 0:
                continue
            # Horizontal floor offsets per metre of camera height above the baskets.
            offsets = [r - (r @ down) * down for r in (first, second)]
            unit = [o / d for o, d in zip(offsets, depth, strict=True)]
            above = separation / np.linalg.norm(unit[1] - unit[0])
            above = above if depth[0] > 0 else -above
            points = [above * u for u in unit]
            x_axis = (points[1] - points[0]) / np.linalg.norm(points[1] - points[0])
            y_axis = np.cross(down, x_axis)
            middle = (points[0] + points[1]) / 2
            # Keep the camera on the near (y > width) side: the image convention.
            if -middle @ y_axis <= 0:
                continue
            basis = np.array([x_axis, y_axis, down])
            centre = np.array([length / 2, width / 2, 0]) - basis @ middle
            centre[2] = -(height + above)
            if not MIN_HEIGHT <= -centre[2] <= MAX_HEIGHT:
                continue
            posts = [
                [length / 6 + BASKET_OFFSET, width / 2, -height],
                [length * 5 / 6 - BASKET_OFFSET, width / 2, -height],
            ]
            baskets = [
                {
                    "keyframe": d["keyframe"],
                    "image": d["image"],
                    "world": post,
                    "foot": d.get("foot"),
                    # The pole stands behind its basket, away from the centre.
                    "post": [length / 6 if n == 0 else length * 5 / 6, width / 2],
                }
                for n, (index, post) in enumerate(zip(order, posts, strict=True))
                for d in pair[index]
            ]
            return {
                "rotation": {k: r @ basis.T for k, r in state["rotation"].items()},
                "focal": dict(state["focal"]),
                "centre": centre,
                "baskets": baskets,
            }
        self.diagnostics.append({
            "members": len(members),
            "failure": "implausible_height",
        })
        return None

    def floor_segments(self, members: list[int], state: dict, placed: dict) -> tuple:
        """Segments whose ends lie on or near the court under a rough placement."""
        _, np = modules()
        centre, margin = placed["centre"], FLOOR_MARGIN
        low = np.array([-margin, -margin])
        high = np.array([self.court["length"], self.court["width"]]) + margin
        normals, owners = [], []
        for k in members:
            for segment in self.keyframes[k]["segments"]:
                reach = rays(
                    np.array(segment), placed["rotation"][k], state["focal"][k]
                )
                if (reach[:, 2] <= EPSILON).any():
                    continue
                floor = centre + (-centre[2] / reach[:, 2])[:, None] * reach
                if ((floor[:, :2] > low) & (floor[:, :2] < high)).all():
                    normals.append(
                        segment_normal(segment, state["rotation"][k], state["focal"][k])
                    )
                    owners.append((k, segment))
        return np.array(normals).reshape(-1, 3), owners

    def floor_families(
        self, members: list[int], state: dict, placed: dict
    ) -> dict | None:
        """Find the floor's two perpendicular line directions.

        Court markings of every sport run along and across the hall. Their
        vanishing directions fix the floor normal and the court's orientation
        from thousands of segments, rather than a few wall edges near the
        image border. The rough placement only selects segments on the floor.
        """
        _, np = modules()
        normals, owners = self.floor_segments(members, state, placed)
        if len(normals) < MIN_FLOOR_SEGMENTS:
            return None
        k0 = members[0]
        # World axes expressed in the reference (first keyframe) frame.
        world = state["rotation"][k0].T @ placed["rotation"][k0]
        found = floor_axes(normals, world[:, 2])
        if found is None:
            return None
        directions, masks = found
        down = np.cross(*directions)
        down /= np.linalg.norm(down)
        if down @ world[:, 2] < 0:
            down = -down
        # Assign each family to the court axis it runs along.
        along = [abs(v @ world[:, 0]) for v in directions]
        axes = (0, 1) if along[0] >= along[1] else (1, 0)
        families = [
            (k, segment, axes[n])
            for n, mask in enumerate(masks)
            for (k, segment), keep in zip(owners, mask, strict=True)
            if keep
        ]
        return {"down": down, "families": families}

    def floor_pieces(self, members: list[int], state: dict, placed: dict) -> list:
        """Every view's segments mapped to the court, as (view, segment, ends)."""
        _, np = modules()
        centre = placed["centre"]
        pieces = []
        for k in members:
            for segment in self.keyframes[k]["segments"]:
                reach = rays(
                    np.array(segment), placed["rotation"][k], state["focal"][k]
                )
                if (reach[:, 2] <= EPSILON).any():
                    continue
                ends = centre[:2] + (-centre[2] / reach[:, 2])[:, None] * reach[:, :2]
                if np.linalg.norm(ends[1] - ends[0]) >= MIN_LINE_PIECE:
                    pieces.append((k, segment, ends))
        return pieces

    def court_lines(
        self, members: list[int], state: dict, placed: dict, window: tuple
    ) -> list:
        """Find the painted end lines, centre line and sidelines near the model.

        Floor segments of every view are mapped to the court. For each template
        line, parallel segments nearby are clustered by offset and the cluster
        with the most painted length wins, if enough views see it. End lines
        and the centre line must also keep their 20 m spacing, so a line of
        another sport cannot pull one end of the court.
        """
        angle, distance = window
        found: dict[tuple, dict] = {}
        for k, segment, ends in self.floor_pieces(members, state, placed):
            step = ends[1] - ends[0]
            length = float(math.hypot(*step))
            for axis, value in court_line_positions(self.court):
                # A line of constant x runs along y, and the reverse.
                offset = float(ends[:, axis].mean()) - value
                span = ends[:, 1 - axis]
                limit = self.court["width" if axis == 0 else "length"]
                if (
                    abs(step[1 - axis]) / length < math.cos(math.radians(angle))
                    or abs(offset) > distance
                    or span.max() < -LINE_WINDOW
                    or span.min() > limit + LINE_WINDOW
                ):
                    continue
                entry = found.setdefault((axis, value), {}).setdefault(
                    round(offset / LINE_BIN), {"length": 0.0, "items": []}
                )
                entry["length"] += length
                entry["items"].append((k, segment, axis, value, offset))
        chosen = consistent({
            line: cluster
            for line, bucket in found.items()
            if (cluster := strongest(bucket)) is not None
        })
        return [
            (k, segment, axis, value)
            for _, items in chosen.values()
            for k, segment, axis, value, _ in items
        ]

    def snap(self, members: list[int], state: dict, placed: dict) -> dict:
        """Snap to the painted court lines, if they agree with the korf posts.

        Wide windows come first: a low camera can leave the heading several
        degrees off. Halls can paint a second set of lines about a metre from
        the korfball court; posts stand 6.67 m from the korfball end lines, so a
        snap that makes the baskets fit clearly worse chose the wrong lines.
        """
        _, np = modules()
        before = float(np.median(self.basket_errors(placed, placed["baskets"])))
        snapped = placed
        for window in LINE_WINDOWS:
            lines = self.court_lines(members, state, snapped, window)
            if not lines:
                break
            snapped = {**snapped, "lines": lines}
            snapped = {**snapped, **self.orient(members, state, snapped)}
        after = float(np.median(self.basket_errors(snapped, snapped["baskets"])))
        if after > max(before * LINE_BASKET_RATIO, LINE_BASKET_PIXELS):
            self.diagnostics.append({
                "members": len(members),
                "rejected_lines": {
                    "baskets_before": round(before, 2),
                    "after": round(after, 2),
                },
            })
            return placed
        return snapped

    def fit(self, members: list[int], state: dict) -> tuple | None:
        """Place the court, refine it on the floor lines and drop stray baskets."""
        placed = self.place(members, state)
        if placed is None:
            return None
        # The hall's line directions give a first vertical; the floor's own
        # lines then fix tilt and heading and the baskets are placed again.
        for _ in range(FLOOR_ROUNDS):
            floor = self.floor_families(members, state, placed)
            if floor is None:
                break
            replaced = self.place(members, state, floor["down"])
            if replaced is None:
                break
            placed = {**replaced, "families": floor["families"]}
            placed = {**placed, **self.orient(members, state, placed)}
        if "families" not in placed:
            self.diagnostics.append({
                "members": len(members),
                "failure": "no_floor_lines",
            })
            return None
        placed = self.snap(members, state, placed)
        solved = self.orient(members, state, placed)
        errors = self.basket_errors(solved, placed["baskets"])
        kept = [
            d
            for d, e in zip(placed["baskets"], errors, strict=True)
            if e <= MAX_BASKET_PIXELS
        ]
        if len(placed["baskets"]) > len(kept) >= 2 * MIN_BASKET_VIEWS:
            placed = {**placed, "baskets": kept, "centre": solved["centre"]}
            solved = self.orient(members, state, placed)
            errors = self.basket_errors(solved, kept)
        if not MIN_HEIGHT <= -float(solved["centre"][2]) <= MAX_HEIGHT or not errors:
            self.diagnostics.append({
                "members": len(members),
                "failure": "implausible_height",
            })
            return None
        return solved, errors

    def basket_errors(self, state: dict, baskets: list[dict]) -> list[float]:
        """Reprojection error of each basket detection in WIDTH pixels."""
        _, np = modules()
        errors = []
        for detection in baskets:
            k = detection["keyframe"]
            image, depth = project(
                np.array([detection["world"][:2]]),
                state["rotation"][k],
                state["focal"][k],
                state["centre"] - [0, 0, detection["world"][2]],
            )
            errors.append(
                math.inf
                if depth[0] <= 0
                else float(np.linalg.norm(image[0] - detection["image"]) * WIDTH)
            )
        return errors
