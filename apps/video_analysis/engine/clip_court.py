"""Reference-guided court registration for moving and returning broadcast views."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from numpy.typing import NDArray

from .clip_signals import modules, transform


NEIGHBOURS = 2
MAX_PROJECTED_EXTENT = 20
MIN_PROJECTED_AREA = 0.01
MIN_SPREAD_X = 100
MIN_SPREAD_Y = 45
MIN_MATCHES = 12
MIN_SUPPORT = 0.55
MAX_ERROR_PIXELS = 2.5
MAX_PROPAGATION_SECONDS = 8
ANCHOR_INTERVAL = 0.5
MIN_LINE_LENGTH = 24
LINE_DISTANCE_PIXELS = 4
MIN_VISIBLE_SAMPLES = 12


class CourtMap:
    """Keep floor-only references; reacquire views without carrying a cut's warp."""

    def __init__(self, court: dict) -> None:
        """Prepare bounded image matchers for one court template."""
        self.cv, self.np = modules()
        self.court = court
        self.orb = self.cv.ORB_create(nfeatures=1600, fastThreshold=10)
        self.matcher = self.cv.BFMatcher(self.cv.NORM_HAMMING)
        self.lines = self.cv.createLineSegmentDetector()
        self.anchors: list[dict[str, Any]] = []
        self.recent: deque = deque(maxlen=4)
        self.last_search = -float("inf")
        self.last_anchor = -float("inf")
        self.floor = None
        self.reference_time = None

    def footprint(self, floor: NDArray[Any]) -> list:
        """Project the physical rectangle into this image, rejecting the horizon.

        Raises:
            ValueError: Projection is degenerate or crosses the horizon.

        """
        inverse = self.np.linalg.inv(floor)
        length, width = self.court["length"], self.court["width"]
        points = [
            transform(p, inverse)
            for p in ((0, 0), (length, 0), (length, width), (0, width))
        ]
        if any(
            p is None or max(abs(v) for v in p) > MAX_PROJECTED_EXTENT for p in points
        ):
            raise ValueError("Court projection crosses the image horizon")
        contour = self.np.float32(points)
        if (
            not self.cv.isContourConvex(contour)
            or abs(self.cv.contourArea(contour)) < MIN_PROJECTED_AREA
        ):
            raise ValueError("Unstable court projection")
        return points

    def features(
        self, image: NDArray[Any], boxes: list, floor: NDArray[Any] | None = None
    ) -> tuple:
        """Exclude people and objects; references additionally exclude the stands."""
        gray = self.cv.cvtColor(
            self.cv.resize(image, (640, 360)), self.cv.COLOR_BGR2GRAY
        )
        mask = self.np.full(gray.shape, 255, dtype=self.np.uint8)
        if floor is not None:
            mask[:] = 0
            polygon = self.np.int32(self.np.array(self.footprint(floor)) * [640, 360])
            self.cv.fillConvexPoly(mask, polygon, 255)
        for x, y, w, h in boxes:
            a = (max(0, int(x * 640) - 4), max(0, int(y * 360) - 4))
            b = (min(639, int((x + w) * 640) + 4), min(359, int((y + h) * 360) + 4))
            self.cv.rectangle(mask, a, b, 0, -1)
        keys, descriptors = self.orb.detectAndCompute(gray, mask)
        return gray, mask, keys, descriptors

    def add_reference(self, image: NDArray[Any], anchor: dict, boxes: list) -> dict:
        """Fit human correspondences and retain their image as an immutable anchor.

        Raises:
            ValueError: Landmarks disagree or produce an unstable mapping.

        """
        source = self.np.float64([p["image"] for p in anchor["points"]])
        target = self.np.float64([p["court"] for p in anchor["points"]])
        target *= [self.court["length"], self.court["width"]]
        floor, _ = self.cv.findHomography(source, target, 0)
        if floor is None or not self.np.isfinite(floor).all():
            raise ValueError("Reference landmarks do not define a court")
        self.footprint(floor)
        projected = self.cv.perspectiveTransform(
            target[None], self.np.linalg.inv(floor)
        )[0]
        errors = self.np.linalg.norm((projected - source) * [640, 360], axis=1)
        if errors.max() > MAX_ERROR_PIXELS * 2:
            raise ValueError("Reference landmarks disagree; check the court points")
        reference = {
            "time": anchor["time"],
            "floor": floor,
            "features": self.features(image, boxes, floor),
        }
        self.anchors.append(reference)
        return {
            "time": anchor["time"],
            "points": len(source),
            "error_pixels": round(float(errors.max()), 3),
        }

    def register(self, current: tuple, reference: tuple) -> tuple | None:
        """Match only distributed floor features with a small reprojection residual."""
        if current[3] is None or reference[3] is None:
            return None
        matches = self.matcher.knnMatch(current[3], reference[3], k=2)
        good = [
            a
            for pair in matches
            if len(pair) == NEIGHBOURS
            for a, b in [pair]
            if a.distance < 0.7 * b.distance
        ]
        if len(good) < MIN_MATCHES:
            return None
        a = self.np.float32([current[2][m.queryIdx].pt for m in good])
        b = self.np.float32([reference[2][m.trainIdx].pt for m in good])
        warp, inliers = self.cv.findHomography(a, b, self.cv.RANSAC, MAX_ERROR_PIXELS)
        if (
            warp is None
            or inliers is None
            or inliers.sum() < MIN_MATCHES
            or inliers.mean() < MIN_SUPPORT
        ):
            return None
        keep = inliers.ravel().astype(bool)
        for points in (a[keep], b[keep]):
            if (
                self.np.ptp(points[:, 0]) < MIN_SPREAD_X
                or self.np.ptp(points[:, 1]) < MIN_SPREAD_Y
            ):
                return None
        projected = self.cv.perspectiveTransform(a[keep][None], warp)[0]
        error = float(self.np.median(self.np.linalg.norm(projected - b[keep], axis=1)))
        if error > MAX_ERROR_PIXELS:
            return None
        scale = self.np.diag([640.0, 360.0, 1.0])
        return self.np.linalg.inv(scale) @ warp @ scale, int(inliers.sum()), error

    def line_evidence(self, features: tuple, floor: NDArray[Any]) -> dict:
        """Measure visible straight-line support for the projected court template."""
        gray, mask = features[:2]
        detected = self.lines.detect(gray)[0]
        canvas = self.np.zeros_like(gray)
        count = 0
        for raw in [] if detected is None else detected.reshape(-1, 4):
            a, b = raw[:2], raw[2:]
            if self.np.linalg.norm(b - a) < MIN_LINE_LENGTH:
                continue
            self.cv.line(canvas, tuple(a.astype(int)), tuple(b.astype(int)), 255, 1)
            count += 1
        canvas[mask == 0] = 0
        distance = self.cv.distanceTransform(255 - canvas, self.cv.DIST_L2, 3)
        length, width = self.court["length"], self.court["width"]
        template = [
            ((0, 0), (length, 0)),
            ((length, 0), (length, width)),
            ((length, width), (0, width)),
            ((0, width), (0, 0)),
            ((length / 2, 0), (length / 2, width)),
        ]
        inverse = self.np.linalg.inv(floor)
        segments = []
        supported = 0
        for a, b in template:
            start, end = transform(a, inverse), transform(b, inverse)
            if start is None or end is None:
                continue
            visible, p, q = self.cv.clipLine(
                (0, 0, 640, 360),
                tuple(int(v * s) for v, s in zip(start, [640, 360], strict=True)),
                tuple(int(v * s) for v, s in zip(end, [640, 360], strict=True)),
            )
            if not visible:
                continue
            points = self.np.linspace(p, q, 100).astype(int)
            points = points[mask[points[:, 1], points[:, 0]] > 0]
            support = (
                float(
                    (distance[points[:, 1], points[:, 0]] < LINE_DISTANCE_PIXELS).mean()
                )
                if len(points) >= MIN_VISIBLE_SAMPLES
                else 0
            )
            supported += int(support >= MIN_SUPPORT)
            segments.append({
                "a": [p[0] / 640, p[1] / 360],
                "b": [q[0] / 640, q[1] / 360],
                "supported": support >= MIN_SUPPORT,
            })
        return {
            "detected_lines": count,
            "supporting_lines": supported,
            "segments": segments,
        }

    def update(
        self, image: NDArray[Any], timestamp: float, boxes: list, cut: bool
    ) -> tuple:
        """Use reference images on either side of a frame; bound incremental drift."""
        current = self.features(image, boxes)
        if cut:
            self.recent.clear()
            self.floor = None
            self.last_search = -float("inf")
        candidate = None
        mode = "unknown"
        evidence = {
            "status": "unknown",
            "reference_time": None,
            "segments": [],
            "supporting_lines": 0,
        }
        if timestamp - self.last_search >= ANCHOR_INTERVAL or self.floor is None:
            self.last_search = timestamp
            for reference in sorted(
                self.anchors, key=lambda r: abs(timestamp - r["time"])
            ):
                match = self.register(current, reference["features"])
                if match:
                    candidate = reference["floor"] @ match[0]
                    self.reference_time = reference["time"]
                    evidence.update(inliers=match[1], error_pixels=round(match[2], 3))
                    mode = "reference"
                    break
        if (
            candidate is None
            and timestamp - self.last_anchor <= MAX_PROPAGATION_SECONDS
        ):
            for reference in reversed(self.recent):
                match = self.register(current, reference["features"])
                if match:
                    candidate = reference["floor"] @ match[0]
                    evidence.update(inliers=match[1], error_pixels=round(match[2], 3))
                    mode = "tracked"
                    break
        evidence["status"] = mode
        candidate, evidence = self.accept(image, boxes, timestamp, candidate, evidence)
        self.floor = candidate
        return candidate, evidence

    def accept(
        self,
        image: NDArray[Any],
        boxes: list,
        timestamp: float,
        candidate: NDArray[Any] | None,
        evidence: dict,
    ) -> tuple:
        """Reject unstable propagation while retaining explicit uncertainty."""
        mode = evidence["status"]
        if candidate is not None:
            try:
                self.footprint(candidate)
                floor_features = self.features(image, boxes, candidate)
                evidence.update(self.line_evidence(floor_features, candidate))
                # Propagation requires visible template-line support; a human
                # reference can still work when floor lines are temporarily hidden.
                if mode == "tracked" and not evidence["supporting_lines"]:
                    candidate = None
                else:
                    if mode == "reference":
                        self.last_anchor = timestamp
                    evidence.update(
                        status=mode,
                        reference_time=self.reference_time,
                        seconds_since_reference=round(timestamp - self.last_anchor, 2),
                    )
                    if (
                        not self.recent
                        or timestamp - self.recent[-1]["time"] >= ANCHOR_INTERVAL
                    ):
                        self.recent.append({
                            "features": floor_features,
                            "floor": candidate,
                            "time": timestamp,
                        })
            except (ValueError, self.np.linalg.LinAlgError):
                candidate = None
        if candidate is None:
            evidence.update(status="unknown", segments=[])
        return candidate, evidence
