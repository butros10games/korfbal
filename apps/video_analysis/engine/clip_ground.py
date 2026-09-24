"""Reject uncertain ground contacts without inventing feet or changing detections."""

from .clip_identity import court_reference
from .clip_signals import distance, transform


MAX_GAP = 0.64
MAX_SPEED = 9
POSITION_TOLERANCE = 0.5
CLIPPED_BOTTOM = 0.995


def hidden_contact(obj: dict, objects: list[dict]) -> bool:
    """Reject feet truncated by the image or hidden inside another body."""
    x, y, width, height = obj["observed_bbox"]
    foot = (x + width / 2, y + height)
    if foot[1] >= CLIPPED_BOTTOM:
        return True
    for other in objects:
        if other is obj:
            continue
        ox, oy, ow, oh = other["observed_bbox"]
        if (
            ox + ow * 0.15 < foot[0] < ox + ow * 0.85
            and oy + oh * 0.2 < foot[1] < oy + oh * 0.85
        ):
            return True
    return False


class GroundContacts:
    """Keep short, camera-compensated contact history for each native track."""

    def __init__(self) -> None:
        """Start with no trusted contacts."""
        self.previous: dict[str, dict] = {}
        self.reference: tuple | None = None

    def update(self, objects: list[dict], timestamp: float, camera: dict) -> None:
        """Withhold uncertain coordinates from replay, association and possession.

        Compare both contacts through the current floor transform, so a refined
        calibration cannot masquerade as player movement. Failed observations do
        not overwrite the last accepted contact. No predicted coordinate is emitted.
        """
        reference = court_reference(camera)
        motion = camera.get("motion")
        if reference != self.reference or motion is None or camera.get("cut"):
            self.previous.clear()
        self.reference = reference
        self.previous = {
            key: state
            for key, state in self.previous.items()
            if state["point"] is not None and 0 <= timestamp - state["time"] <= MAX_GAP
        }
        if motion is not None:
            for state in self.previous.values():
                state["point"] = transform(state["point"], motion)
        for obj in objects:
            position = obj.get("court_xy_m")
            if position is None:
                continue
            x, y, width, height = obj["observed_bbox"]
            point = [x + width / 2, y + height]
            prior = self.previous.get(obj["track_id"])
            issue = "occluded_ground_contact" if hidden_contact(obj, objects) else None
            if not issue and prior and prior["point"] is not None and reference:
                projected = transform(prior["point"], camera["floor"])
                dt = timestamp - prior["time"]
                if projected is not None and distance(projected, position) > (
                    POSITION_TOLERANCE + MAX_SPEED * dt
                ):
                    issue = "implausible_ground_motion"
            if issue:
                obj["court_xy_m"] = None
                obj["ground_issue"] = issue
            elif reference:
                self.previous[obj["track_id"]] = {"point": point, "time": timestamp}
