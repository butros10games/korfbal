"""Geometric court-half context, independent of team or attack/defence roles."""

MIDLINE_MARGIN = 0.6
MIN_SAMPLES = 3
MIN_SECONDS = 0.15
MAX_GAP = 0.4


class CourtHalves:
    """Stabilize measured half membership while allowing actual crossings."""

    def __init__(self, length: float = 40, width: float = 20) -> None:
        """Use configured court dimensions; unknown calibration has no half context."""
        self.length = length
        self.width = width
        self.states: dict[str, dict] = {}
        self.key = None

    def side(self, point: list | None) -> str:
        """Abstain near the centre line rather than flipping on projection noise."""
        if (
            point is None
            or not 0 <= point[0] <= self.length
            or not 0 <= point[1] <= self.width
        ):
            return "unknown"
        if point[0] < self.length / 2 - MIDLINE_MARGIN:
            return "left"
        if point[0] > self.length / 2 + MIDLINE_MARGIN:
            return "right"
        return "centre"

    def update(self, objects: list, timestamp: float, key: object) -> None:
        """Only measured observations establish or change a player's half group."""
        if key is None or key != self.key:
            self.states.clear()
        self.key = key
        self.states = {
            k: v for k, v in self.states.items() if timestamp - v["last"] <= MAX_GAP
        }
        for obj in objects:
            side = self.side(obj.get("court_xy_m")) if key is not None else "unknown"
            obj["court_half"] = side
            identity = obj["track_id"]
            state: dict | None = self.states.get(identity)
            if side in {"left", "right"} and not obj.get("identity_uncertain"):
                if state is None or state["candidate"] != side:
                    state = {
                        "candidate": side,
                        "start": timestamp,
                        "count": 0,
                        "member": state["member"] if state else "unknown",
                    }
                state["count"] += 1
                state["last"] = timestamp
                if (
                    state["count"] >= MIN_SAMPLES
                    and timestamp - state["start"] >= MIN_SECONDS
                ):
                    state["member"] = side
                self.states[identity] = state
            obj["half_group"] = state["member"] if state else "unknown"

    def penalty(self, identity: str, obj: dict, key: object, timestamp: float) -> float:
        """Use stable half groups as soft association evidence, never a crossing ban."""
        state: dict | None = self.states.get(identity)
        side = self.side(obj.get("court_xy_m"))
        if (
            key is None
            or key != self.key
            or state is None
            or timestamp - state["last"] > MAX_GAP
            or state["member"] not in {"left", "right"}
        ):
            return 0.0
        return 0.35 if side in {"left", "right"} and side != state["member"] else 0.0
