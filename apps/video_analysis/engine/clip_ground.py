"""Reject uncertain ground contacts without inventing feet or changing detections."""

from .boxes import iou
from .clip_identity import court_reference
from .clip_signals import distance, transform


MAX_GAP = 0.64
MAX_SPEED = 9
POSITION_TOLERANCE = 0.5
CLIPPED_BOTTOM = 0.995
# Two boxes this overlapped, ending on nearly the same floor line, are one body
# standing behind another (stacked dots on the map in benchmark clips).
STACKED_IOU = 0.4
STACKED_BOTTOM = 0.06
SAME_HEIGHT = 0.03


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
        ) or behind(obj, other):
            return True
    return False


def behind(obj: dict, other: dict) -> bool:
    """Whether `obj` stands behind `other`, its box drawn down to the same floor line.

    The detector completes a partly hidden body to the front player's feet, so
    both contacts land on one spot and two dots stack on the map. The rear body
    is the smaller (farther) box; at equal size, the less certain detection.
    """
    a, b = obj["observed_bbox"], other["observed_bbox"]
    if iou(a, b) < STACKED_IOU or abs((a[1] + a[3]) - (b[1] + b[3])) > (
        STACKED_BOTTOM * max(a[3], b[3])
    ):
        return False
    if abs(a[3] - b[3]) > SAME_HEIGHT * max(a[3], b[3]):
        return a[3] < b[3]
    return obj.get("confidence", 0) < other.get("confidence", 0)


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
                withhold(obj, issue)
            elif reference:
                self.previous[obj["track_id"]] = {"point": point, "time": timestamp}


def withhold(obj: dict, issue: str) -> None:
    """Remove an unreliable contact from measurements, keeping a display estimate.

    The box bottom behind another body is usually within a metre of the player,
    so the replay may show it as an estimate; it never counts as a measurement.
    """
    position = obj["court_xy_m"]
    obj["court_xy_m"] = None
    obj["ground_issue"] = issue
    if issue == "occluded_ground_contact":
        _, y, _, height = obj["observed_bbox"]
        obj["contact_estimate_xy_m"] = position
        obj["contact_clipped"] = y + height >= CLIPPED_BOTTOM
