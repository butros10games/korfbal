"""Short-lived torso and hip optical flow, independent of visible feet."""

from typing import Any

from .clip_signals import modules


MAX_AGE = 0.48
MIN_POINTS = 4
MAX_FRAME_GAP = 0.2
MAX_REVERSE_ERROR = 1.5
MAX_FLOW_DEVIATION = 3
TORSO_END = 0.45
HIP_START = 0.48
MIN_BAND_POINTS = 2


class BodyMotion:
    """Track non-overlapping clothing features; never manufacture pose keypoints."""

    def __init__(self) -> None:
        """Retain one grayscale frame and bounded feature sets for one segment."""
        self.gray: Any = None
        self.time: float | None = None
        self.tracks: dict[str, dict] = {}

    def predict(self, image: object, time: float) -> dict:
        """Require forward/backward flow agreement before contributing body motion."""
        cv, np = modules()
        gray = cv.cvtColor(image, cv.COLOR_BGR2GRAY)
        result = {}
        if (
            self.gray is not None
            and self.gray.shape == gray.shape
            and self.time is not None
            and 0 < time - self.time <= MAX_FRAME_GAP
        ):
            for identity, state in list(self.tracks.items()):
                if time - state["seed_time"] > MAX_AGE:
                    continue
                points = state["points"]
                moved, valid, _ = cv.calcOpticalFlowPyrLK(self.gray, gray, points, None)
                if moved is None:
                    continue
                back, reverse, _ = cv.calcOpticalFlowPyrLK(gray, self.gray, moved, None)
                if back is None:
                    continue
                good = (valid.ravel() > 0) & (reverse.ravel() > 0)
                good &= (
                    np.linalg.norm(back[:, 0] - points[:, 0], axis=1)
                    < MAX_REVERSE_ERROR
                )
                good &= np.isfinite(moved[:, 0]).all(axis=1)
                if good.sum() < MIN_POINTS:
                    continue
                delta = np.median(moved[good, 0] - points[good, 0], axis=0)
                coherent = (
                    np.linalg.norm(moved[:, 0] - points[:, 0] - delta, axis=1)
                    < MAX_FLOW_DEVIATION
                )
                good &= coherent
                if good.sum() < MIN_POINTS:
                    continue
                delta = np.median(moved[good, 0] - points[good, 0], axis=0)
                state["points"] = moved[good]
                state["anchors"] += delta
                h, w = gray.shape
                result[identity] = (state["anchors"] / [w, h]).tolist()
        self.tracks = {k: v for k, v in self.tracks.items() if k in result}
        self.gray, self.time = gray, time
        return result

    def seed(self, objects: list[dict], time: float) -> None:
        """Sample torso and hip bands, masking all other observed people's boxes."""
        cv, np = modules()
        if self.gray is None:
            return
        h, w = self.gray.shape
        for obj in objects:
            if obj.get("identity_status") == "pending" or obj.get("identity_uncertain"):
                continue
            x, y, bw, bh = obj["observed_bbox"]
            mask = np.zeros_like(self.gray)
            for top, bottom in ((0.18, 0.45), (0.48, 0.68)):
                cv.rectangle(
                    mask,
                    (round((x + 0.2 * bw) * w), round((y + top * bh) * h)),
                    (round((x + 0.8 * bw) * w), round((y + bottom * bh) * h)),
                    255,
                    -1,
                )
            for other in objects:
                if other is obj:
                    continue
                a, b, c, d = other["observed_bbox"]
                cv.rectangle(
                    mask,
                    (round(a * w), round(b * h)),
                    (round((a + c) * w), round((b + d) * h)),
                    0,
                    -1,
                )
            points = cv.goodFeaturesToTrack(self.gray, 24, 0.02, 4, mask=mask)
            if points is None or len(points) < MIN_POINTS:
                continue
            # Require both bands; a single background corner is insufficient.
            ys = (points[:, 0, 1] / h - y) / max(bh, 1e-9)
            if (
                sum(ys < TORSO_END) < MIN_BAND_POINTS
                or sum(ys > HIP_START) < MIN_BAND_POINTS
            ):
                continue
            self.tracks[obj["track_id"]] = {
                "points": points,
                "seed_time": time,
                "anchors": np.array([
                    [x + bw / 2, y + bh * 0.32],
                    [x + bw / 2, y + bh * 0.58],
                ])
                * [w, h],
            }
