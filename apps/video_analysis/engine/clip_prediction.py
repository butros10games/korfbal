"""Bounded replay predictions, separate from measured association/event evidence."""

from itertools import pairwise

from .clip_identity import court_reference
from .clip_signals import distance, modules, transform


MAX_PREDICTION_GAP = 0.64
MAX_SAMPLE_GAP = 0.24
MIN_HISTORY = 0.15
MIN_SAMPLES = 3
MAX_SPEED = 9


class PositionMemory:
    """Retain reliable feet by public identity and bridge briefly hidden feet."""

    def __init__(self) -> None:
        """Start with no observations in a camera coordinate system."""
        self.samples: dict[str, list[tuple[float, list]]] = {}
        self.reference: tuple | None = None

    def update(self, objects: list[dict], timestamp: float, camera: dict) -> None:
        """Predict only for a currently detected player with three reliable samples.

        Warp every historical foot into the current camera before measuring
        velocity. Predictions never feed back into history or court_xy_m, and
        expire relative to the last real contact, not the last prediction.
        """
        self.advance(timestamp, camera)
        if self.reference is None:
            return
        for obj in objects:
            obj.pop("court_prediction", None)
            if obj["label"] != "player":
                continue
            identity = obj["track_id"]
            if obj.get("identity_uncertain"):
                self.samples.pop(identity, None)
                continue
            history = self.samples.get(identity, [])
            if obj.get("court_xy_m") is not None:
                x, y, w, h = obj["observed_bbox"]
                if history and timestamp - history[-1][0] > MAX_SAMPLE_GAP:
                    history = []
                self.samples[identity] = [*history, (timestamp, [x + w / 2, y + h])][
                    -5:
                ]
            elif obj.get("ground_issue") == "occluded_ground_contact":
                prediction = self.predict(obj, history, timestamp, camera)
                if prediction is not None:
                    obj["court_prediction"] = prediction

    def advance(self, timestamp: float, camera: dict) -> None:
        """Age and warp only real samples within the same camera reference."""
        reference = court_reference(camera)
        motion = camera.get("motion")
        if reference != self.reference or motion is None or camera.get("cut"):
            self.samples.clear()
        self.reference = reference
        if reference is None or motion is None:
            return
        warped = {}
        for identity, samples in self.samples.items():
            if not 0 < timestamp - samples[-1][0] <= MAX_PREDICTION_GAP:
                continue
            points = [
                (t, projected)
                for t, p in samples
                if (projected := transform(p, motion)) is not None
            ]
            if len(points) == len(samples):
                warped[identity] = points
        self.samples = warped

    @staticmethod
    def predict(
        obj: dict, history: list, timestamp: float, camera: dict
    ) -> dict | None:
        """Require bounded speed and agreement with the currently visible body."""
        if len(history) < MIN_SAMPLES or history[-1][0] - history[0][0] < MIN_HISTORY:
            return None
        points = [
            projected
            for _, p in history
            if (projected := transform(p, camera["floor"])) is not None
        ]
        if len(points) != len(history):
            return None
        velocities = [
            [(b[k] - a[k]) / (history[i + 1][0] - history[i][0]) for k in (0, 1)]
            for i, (a, b) in enumerate(pairwise(points))
        ]
        if any(distance(v, [0, 0]) > MAX_SPEED for v in velocities):
            return None
        _, np = modules()
        velocity = np.median(velocities, axis=0)
        age = timestamp - history[-1][0]
        xy = [float(points[-1][k] + velocity[k] * age) for k in (0, 1)]
        try:
            foot = transform(xy, np.linalg.inv(camera["floor"]))
        except np.linalg.LinAlgError:
            return None
        x, y, w, h = obj["observed_bbox"]
        if foot is None or not (
            x - w * 0.25 <= foot[0] <= x + w * 1.25
            and y + h * 0.5 <= foot[1] <= y + h * 1.8
        ):
            return None
        return {
            "xy": xy,
            "position_source": "motion_prediction",
            # Display support, not a calibrated probability of correctness.
            "position_confidence": round(0.7 - 0.5 * age / MAX_PREDICTION_GAP, 3),
            "age_seconds": round(age, 3),
        }
