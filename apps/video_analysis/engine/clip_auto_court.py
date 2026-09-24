"""Estimate a floor from an observed penalty circle, spot and basket-supported pole.

These are automatic estimates, not reviewed references. Missing or ambiguous cues
produce no map. A basket box or a coloured oval alone cannot define a court.
"""

from __future__ import annotations

from operator import itemgetter
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from numpy.typing import NDArray

from . import (
    clip_flow,
    clip_geometry as geometry,
)
from .clip_auto_references import References
from .clip_court import CourtMap
from .clip_signals import modules


SEARCH_INTERVAL = 0.48
MAX_HOLD = 4.0
MIN_CIRCLE_SUPPORT = 0.72
MIN_POLE_SUPPORT = 0.6
RADIUS = 2.5
MAX_CENTRE_OFFSET = 0.45
MIN_INK = 18
MIN_SPOT_INK = 12
MIN_BASKET_CONFIDENCE = 0.45
MIN_ARC_POINTS = 60
MIN_ASPECT = 0.1
MAX_ASPECT = 0.8
MAX_RADIAL_ERROR = 0.04
MIN_CIRCLE_SAMPLES = 90
MAX_EDGE_DISTANCE = 3
DUPLICATE_CENTRE = 8
DUPLICATE_AXES = 10
MIN_SPOT_RATIO = 0.008
MAX_SPOT_WIDTH = 0.04
MAX_SPOT_HEIGHT = 0.08
MIN_SPOT_AREA = 5
MAX_SPOT_RADIAL = 0.4
SPOT_MARGIN = 0.12
MIN_SHAFT_LENGTH = 75
MIN_SHAFT_SAMPLES = 40
POLE_AMBIGUITY_DISTANCE = 20


def project(matrix: NDArray[Any], points: list | NDArray[Any]) -> NDArray[Any]:
    """Project a small set of finite diagnostic points."""
    _, np = modules()
    q = np.c_[points, np.ones(len(points))] @ matrix.T
    return q[:, :2] / q[:, 2:]


def circle_plane(
    ellipse: tuple,
    spot: list | NDArray[Any],
    pole: list | NDArray[Any],
    court: dict,
    shape: tuple,
) -> NDArray[Any]:
    """Rectify a circle using its observed physical centre and pole direction.

    Apparent ellipse extrema are not court-axis correspondences. A projective
    disk transformation brings the physical centre to the circle's centre.

    Raises:
        ValueError: Centre or pole orientation is ambiguous.

    """
    _, np = modules()
    (cx, cy), (a, b), angle = ellipse
    theta = np.deg2rad(angle)
    rotation = np.array([
        [np.cos(theta), np.sin(theta)],
        [-np.sin(theta), np.cos(theta)],
    ])
    affine = np.diag([2 / a, 2 / b]) @ rotation
    ellipse_map = np.eye(3)
    ellipse_map[:2, :2], ellipse_map[:2, 2] = affine, -affine @ [cx, cy]
    centre = project(ellipse_map, [spot])[0]
    radius = float(np.linalg.norm(centre))
    if radius > MAX_CENTRE_OFFSET or abs(pole[0] - spot[0]) < max(a, b) * 0.2:
        raise ValueError("Penalty direction is ambiguous")
    gamma = 1 / np.sqrt(1 - radius**2)
    boost = np.eye(3)
    if radius:
        boost[:2, :2] += (gamma - 1) * np.outer(centre, centre) / radius**2
    boost[:2, 2] = boost[2, :2] = -gamma * centre
    boost[2, 2] = gamma
    pole_direction = project(boost @ ellipse_map, [pole])[0]
    left = pole[0] < spot[0]
    angle = (np.pi if left else 0) - np.arctan2(pole_direction[1], pole_direction[0])
    turn = np.eye(3)
    turn[:2, :2] = [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]]
    floor = turn @ boost @ ellipse_map
    vertical = project(np.linalg.inv(floor), [[0, 0], [0, 0.1]])
    if vertical[1, 1] < vertical[0, 1]:
        floor[1] *= -1
    post_x = court["length"] * (1 / 6 if left else 5 / 6)
    spot_x = post_x + (RADIUS if left else -RADIUS)
    floor = (
        np.array([[RADIUS, 0, spot_x], [0, RADIUS, court["width"] / 2], [0, 0, 1]])
        @ floor
    )
    floor @= np.diag([shape[1], shape[0], 1])
    floor = geometry.orient(floor, np.array([spot, pole]) / [shape[1], shape[0]])
    geometry.footprint(floor, court)
    return floor


class Landmarks:
    """Bounded image measurements, with independent circle, spot and shaft tests."""

    def __init__(self, image: NDArray[Any], objects: list) -> None:
        """Analyze at most 1280 pixels across, without changing detector resolution."""
        self.cv, self.np = modules()
        scale = min(1, 1280 / image.shape[1])
        self.image = self.cv.resize(image, None, fx=scale, fy=scale)
        self.gray = self.cv.cvtColor(self.image, self.cv.COLOR_BGR2GRAY)
        self.height, self.width = self.gray.shape
        self.mask = self.np.full(self.gray.shape, 255, dtype=self.np.uint8)
        for obj in objects:
            if obj["label"] not in {"player", "referee"}:
                continue
            x, y, w, h = self.np.array(obj["bbox"]) * [
                self.width,
                self.height,
                self.width,
                self.height,
            ]
            self.cv.rectangle(
                self.mask,
                (int(x) - 4, int(y) - 4),
                (int(x + w) + 4, int(y + h) + 4),
                0,
                -1,
            )
        geometry.exclude_overlays(self.mask)
        self.mask[: round(self.height * 0.3)] = 0
        ink_mask = self.mask.copy()
        # A dark shaft touching the circle must not become part of its contour.
        for obj in objects:
            if obj["label"] == "basket":
                x, y, w, _ = self.np.array(obj["bbox"]) * [
                    self.width,
                    self.height,
                    self.width,
                    self.height,
                ]
                self.cv.rectangle(
                    ink_mask,
                    (max(0, int(x - w * 0.4)), int(y)),
                    (min(self.width - 1, int(x + w * 1.4)), self.height - 1),
                    0,
                    -1,
                )
        kernel = max(9, round(self.width * 0.016) | 1)
        blackhat = self.cv.morphologyEx(
            self.gray,
            self.cv.MORPH_BLACKHAT,
            self.cv.getStructuringElement(self.cv.MORPH_ELLIPSE, (kernel, kernel)),
        )
        self.ink = self.np.uint8((blackhat > MIN_INK) & (ink_mask > 0)) * 255
        self.ink_mask = ink_mask
        self.distance = self.cv.distanceTransform(255 - self.ink, self.cv.DIST_L2, 3)
        _, _, stats, centres = self.cv.connectedComponentsWithStats(
            self.np.uint8((blackhat > MIN_SPOT_INK) & (ink_mask > 0))
        )
        self.spots = list(zip(stats[1:], centres[1:], strict=True))
        edges = self.cv.Canny(self.gray, 35, 100)
        self.pole_distance = self.cv.distanceTransform(255 - edges, self.cv.DIST_L2, 3)
        lines = self.cv.HoughLinesP(
            edges, 1, self.np.pi / 720, 20, minLineLength=50, maxLineGap=25
        )
        self.lines = [] if lines is None else lines.reshape(-1, 4).tolist()
        self.baskets = [
            o
            for o in objects
            if o["label"] == "basket"
            and o.get("confidence", 0) >= MIN_BASKET_CONFIDENCE
        ][:4]

    def ellipse_map(self, ellipse: tuple) -> NDArray[Any]:
        """Map ellipse pixels to a unit circle for cue selection."""
        (cx, cy), (a, b), angle = ellipse
        rotation = self.cv.getRotationMatrix2D((cx, cy), angle, 1)
        result = self.np.eye(3)
        result[:2] = rotation
        result[:2, 2] -= [cx, cy]
        return self.np.diag([2 / a, 2 / b, 1]) @ result

    def circles(self, contours: list[NDArray[Any]] | None = None) -> list:
        """Fit visible black arcs and verify support around their inferred ellipse."""
        if contours is None:
            raw_contours, _ = self.cv.findContours(
                self.ink, self.cv.RETR_LIST, self.cv.CHAIN_APPROX_NONE
            )
            measured: list[NDArray[Any]] = list(raw_contours)
            contours = sorted(measured, key=len, reverse=True)[:40]
        found = []
        for contour in contours:
            if len(contour) < MIN_ARC_POINTS:
                continue
            ellipse = self.cv.fitEllipse(contour)
            (cx, cy), axes, _ = ellipse
            minor, major = sorted(axes)
            if not (
                self.width * 0.12 < major < self.width * 0.85
                and self.height * 0.04 < minor < self.height * 0.5
                and MIN_ASPECT < minor / major < MAX_ASPECT
                and self.height * 0.3 < cy < self.height * 0.9
                and 0 < cx < self.width
            ):
                continue
            matrix = self.ellipse_map(ellipse)
            radial = self.np.linalg.norm(project(matrix, contour[:, 0, :]), axis=1)
            if self.np.median(abs(radial - 1)) > MAX_RADIAL_ERROR:
                continue
            angles = self.np.linspace(0, 2 * self.np.pi, 180, endpoint=False)
            pixels = (
                project(
                    self.np.linalg.inv(matrix),
                    self.np.c_[self.np.cos(angles), self.np.sin(angles)],
                )
                .round()
                .astype(int)
            )
            inside = (
                (pixels[:, 0] >= 0)
                & (pixels[:, 0] < self.width)
                & (pixels[:, 1] >= 0)
                & (pixels[:, 1] < self.height)
            )
            pixels = pixels[inside]
            pixels = pixels[self.mask[pixels[:, 1], pixels[:, 0]] > 0]
            if len(pixels) < MIN_CIRCLE_SAMPLES:
                continue
            support = float(
                (self.distance[pixels[:, 1], pixels[:, 0]] <= MAX_EDGE_DISTANCE).mean()
            )
            if support >= MIN_CIRCLE_SUPPORT:
                if any(
                    self.np.linalg.norm(self.np.array(ellipse[0]) - other[0][0])
                    < DUPLICATE_CENTRE
                    and max(abs(self.np.array(ellipse[1]) - other[0][1]))
                    < DUPLICATE_AXES
                    for other in found
                ):
                    continue
                found.append((ellipse, support))
        return found

    def spot(self, ellipse: tuple, *, pixel_limit: int = 0) -> NDArray[Any] | None:
        """Require a small isolated mark near the circle's perspective centre."""
        minor, major = sorted(ellipse[1])
        candidates: list = []
        matrix = self.ellipse_map(ellipse)
        for (_, _, w, h, area), centre in self.spots:
            radial = float(self.np.linalg.norm(project(matrix, [centre])[0]))
            small = w < max(major * MAX_SPOT_WIDTH, pixel_limit) and h < max(
                minor * MAX_SPOT_HEIGHT, pixel_limit
            )
            if (
                w / major > MIN_SPOT_RATIO
                and h / minor > MIN_SPOT_RATIO
                and small
                and area >= MIN_SPOT_AREA
                and radial < MAX_SPOT_RADIAL
            ):
                candidates.append((radial, centre))
        candidates.sort(key=itemgetter(0))
        if not candidates or (
            len(candidates) > 1 and candidates[1][0] - candidates[0][0] < SPOT_MARGIN
        ):
            return None
        return candidates[0][1]

    def shaft_support(
        self, top: float, bottom: float, slope: float, intercept: float
    ) -> float:
        """Measure visible shaft edges, excluding occlusion and off-image pixels."""
        ys = self.np.linspace(top + 5, bottom - 4, 100)
        points = self.np.c_[slope * ys + intercept, ys].astype(int)
        inside = (
            (points[:, 0] >= 0)
            & (points[:, 0] < self.width)
            & (points[:, 1] >= 0)
            & (points[:, 1] < self.height)
        )
        points = points[inside]
        visible = self.mask[points[:, 1], points[:, 0]] > 0
        visible |= points[:, 1] < self.height * 0.3
        if visible.sum() < MIN_SHAFT_SAMPLES:
            return 0.0
        points = points[visible]
        return float(
            (self.pole_distance[points[:, 1], points[:, 0]] <= MAX_EDGE_DISTANCE).mean()
        )

    def pole(self, ellipse: tuple) -> tuple | None:
        """Intersect a supported basket shaft with the rear penalty-circle edge."""
        matrix = self.ellipse_map(ellipse)
        candidates: list = []
        for raw in self.lines:
            (x1, y1), (x2, y2) = sorted((raw[:2], raw[2:]), key=itemgetter(1))
            if y2 - y1 < MIN_SHAFT_LENGTH or abs(x2 - x1) > (y2 - y1) * 0.15:
                continue
            slope = (x2 - x1) / (y2 - y1)
            intercept = x1 - slope * y1
            u, v = (matrix @ [slope, 1, 0])[:2], (matrix @ [intercept, 0, 1])[:2]
            roots = self.np.roots([u @ u, 2 * (u @ v), v @ v - 1])
            if not self.np.isreal(roots).all():
                continue
            y = float(min(roots.real))
            x = slope * y + intercept
            if (
                not (0 <= x < self.width and 0 <= y < self.height)
                or not self.mask[int(y), int(x)]
            ):
                continue
            for basket in self.baskets:
                bx, by, bw, bh = self.np.array(basket["bbox"]) * [
                    self.width,
                    self.height,
                    self.width,
                    self.height,
                ]
                top = by + bh
                at_basket = slope * top + intercept
                if not (
                    bx - 5 < at_basket < bx + bw + 5
                    and top + 100 < y < top + self.height * 0.75
                    and y1 < top + 170
                    and y2 > y - 180
                ):
                    continue
                support = self.shaft_support(top, y, slope, intercept)
                if support >= MIN_POLE_SUPPORT:
                    candidates.append((support, [x, y]))
        candidates.sort(key=itemgetter(0), reverse=True)
        if not candidates:
            return None
        if any(
            self.np.linalg.norm(self.np.array(p) - candidates[0][1])
            > POLE_AMBIGUITY_DISTANCE
            and support > candidates[0][0] - 0.1
            for support, p in candidates[1:]
        ):
            return None
        return candidates[0]


def estimate(image: NDArray[Any], objects: list, court: dict) -> tuple | None:
    """Return explicit cue provenance, or abstain when no unique fit exists."""
    if not any(o["label"] == "basket" for o in objects):
        return None
    observed = Landmarks(image, objects)
    candidates = []
    for ellipse, support in observed.circles():
        spot, pole = observed.spot(ellipse), observed.pole(ellipse)
        if spot is None or pole is None:
            continue
        try:
            floor = circle_plane(ellipse, spot, pole[1], court, observed.gray.shape)
        except (ValueError, observed.np.linalg.LinAlgError):
            continue
        candidates.append((
            floor,
            {
                "status": "automatic",
                "estimated": True,
                "circle_support": round(support, 3),
                "pole_support": round(pole[0], 3),
                "observed_spot": (spot / [observed.width, observed.height]).tolist(),
                "observed_pole": (
                    observed.np.array(pole[1]) / [observed.width, observed.height]
                ).tolist(),
                "segments": [],
            },
        ))
    if len(candidates) != 1:
        return None
    return candidates[0]


class AutoCourt:
    """Reacquire verified views and bridge short gaps with floor-only motion."""

    def __init__(self, court: dict) -> None:
        """Keep original reference images; no propagated mapping survives a cut."""
        self.court = court
        self.matcher = CourtMap(court)
        self.references = References(self.matcher)
        self.last_search = -float("inf")
        self.last_seen = -float("inf")
        self.previous = None
        self.floor = None
        self.evidence = {}

    def observe(
        self, image: NDArray[Any], timestamp: float, objects: list
    ) -> tuple | None:
        """Prefer a verified original view over refitting partly hidden landmarks."""
        found = self.references.find(image, timestamp, objects)
        if found is None:
            found = estimate(image, objects, self.court)
            if found:
                self.references.add(image, timestamp, objects, found)
        return found

    def update(
        self, image: NDArray[Any], timestamp: float, objects: list, cut: bool
    ) -> tuple:
        """Persist only a fresh estimate or a well-supported short propagation."""
        if cut:
            self.floor, self.previous = None, None
            self.last_search = -float("inf")
        boxes = [o["bbox"] for o in objects]
        candidate = None
        evidence = {"status": "unknown", "segments": [], "estimated": True}
        if timestamp - self.last_search >= SEARCH_INTERVAL or self.floor is None:
            self.last_search = timestamp
            found = self.observe(image, timestamp, objects)
            if found:
                candidate, evidence = found
                self.last_seen = timestamp
        if (
            candidate is None
            and self.floor is not None
            and self.previous
            and timestamp - self.last_seen <= MAX_HOLD
        ):
            current = self.matcher.features(image, boxes, self.floor)
            match = clip_flow.register(self.previous, current)
            if match:
                candidate = self.floor @ match[0]
                evidence = {
                    **self.evidence,
                    "status": "automatic_tracked",
                    "seconds_since_observation": round(timestamp - self.last_seen, 2),
                    "inliers": match[1],
                }
        if candidate is not None:
            try:
                features = self.matcher.features(image, boxes, candidate)
                self.previous = features
                self.evidence = evidence
            except ValueError:
                candidate = None
        if candidate is None:
            self.previous = None
            evidence = {"status": "unknown", "segments": [], "estimated": True}
        self.floor = candidate
        return candidate, evidence
