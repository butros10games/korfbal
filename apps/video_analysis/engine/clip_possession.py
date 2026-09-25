"""Review-only control and team-change hypotheses from repeated observations.

Image proximity and co-motion cannot establish possession or fault. Keep both
players as candidates, distinguish shot recoveries, and abstain across lost data.
After a shot, the attacking team decides whether a recovery is an offensive or
defensive rebound; after a possible goal it is only a restart candidate.
"""

from collections import deque
from copy import deepcopy

from .clip_ball_chain import BallFrame
from .clip_events import MAX_EVENTS, TEAMS, box, centre


CONTROL_SECONDS = 0.24
CONTROL_OBSERVATIONS = 4
CONTROL_WINDOW = 0.48
MAX_RELATIVE_X_SPREAD = 0.4
MAX_RELATIVE_Y_SPREAD = 0.3
MAX_TRANSFER_SECONDS = 3
SHOT_CONTEXT_SECONDS = 4
COURT_MARGIN = 0.5
RELATIVE_X = (-0.15, 1.15)
RELATIVE_Y = (-0.1, 0.8)
CONTROL_X = (0.2, 0.8)
CONTROL_Y = (0.15, 0.7)
VERSION = 2


def player_candidate(
    objects: list[dict], ball: dict, court: dict | None
) -> tuple | None:
    """Choose a unique observed upper body; court coordinates are optional."""
    point = centre(box(ball))
    candidates = []
    for obj in objects:
        if obj["label"] != "player" or obj.get("estimated") or not obj.get("track_id"):
            continue
        xy = obj.get("court_xy_m")
        dimensions = court or {"length": 40, "width": 20}
        if xy is not None and not all(
            -COURT_MARGIN <= v <= dimensions[k] + COURT_MARGIN
            for v, k in zip(xy, ("length", "width"), strict=True)
        ):
            continue
        x, y, w, h = box(obj)
        if min(w, h) <= 0:
            continue
        relative = ((point[0] - x) / w, (point[1] - y) / h)
        if not (
            RELATIVE_X[0] <= relative[0] <= RELATIVE_X[1]
            and RELATIVE_Y[0] <= relative[1] <= RELATIVE_Y[1]
        ):
            continue
        candidates.append((obj, relative))
    # Distance cannot resolve who is in front when two bodies overlap the ball.
    # Outstretched/overhead boxes also frequently belong to the defender covering
    # an undetected holder. Require central-body evidence to acquire control.
    if len(candidates) != 1 or candidates[0][0].get("identity_uncertain"):
        return None
    obj, relative = candidates[0]
    if not (
        CONTROL_X[0] <= relative[0] <= CONTROL_X[1]
        and CONTROL_Y[0] <= relative[1] <= CONTROL_Y[1]
    ):
        return None
    player = {
        k: deepcopy(obj.get(k))
        for k in ("track_id", "display_id", "team", "court_xy_m")
    }
    player["team"] = player["team"] if player["team"] in TEAMS else "unknown"
    return player, relative


def recovery_type(shot: dict, attacking: str, gaining: str) -> str:
    """Classify who recovered a shot; a possible goal makes it a restart candidate."""
    if shot["outcome"] == "possible_goal":
        return "restart_after_possible_goal"
    if attacking not in TEAMS:
        return "unknown"
    return "offensive_rebound" if attacking == gaining else "defensive_rebound"


class PossessionEvents:
    """Require sustained control before attributing a team loss and gain."""

    def __init__(self, court: dict | None = None) -> None:
        """Bound observations and events independently from shot detection."""
        self.events: list[dict] = []
        self.court = court
        self.streak: deque[dict] = deque(maxlen=16)
        self.holder: dict | None = None
        self.truncated = False

    def reset(self) -> None:
        """Break attribution without manufacturing a loss when evidence vanishes."""
        self.streak.clear()
        self.holder = None

    def update(self, frame: BallFrame, shots: list[dict]) -> dict:
        """Return current visible control evidence and collect resolved transitions."""
        ball, timestamp = frame.ball, frame.timestamp
        if self.holder and timestamp - self.holder["last_seen"] > MAX_TRANSFER_SECONDS:
            self.holder = None
        candidate = player_candidate(frame.objects, ball, self.court) if ball else None
        if candidate is None:
            self.streak.clear()
            return {"status": "unknown", "holder_candidate": None}
        player, relative = candidate
        if self.streak and any(
            player[k] != self.streak[-1]["player"][k] for k in ("track_id", "team")
        ):
            self.streak.clear()
        cadence = timestamp - self.streak[-1]["time"] if self.streak else 0
        window = max(CONTROL_WINDOW, cadence * (CONTROL_OBSERVATIONS - 1))
        self.streak.append({"player": player, "relative": relative, "time": timestamp})
        while self.streak and timestamp - self.streak[0]["time"] > window + 1e-6:
            self.streak.popleft()
        while self.streak and not self.steady():
            self.streak.popleft()
        enough = (
            len(self.streak) >= CONTROL_OBSERVATIONS
            and timestamp - self.streak[0]["time"] >= CONTROL_SECONDS - 1e-6
        )
        if not enough:
            return {"status": "unknown", "holder_candidate": None}
        self.accept(player, frame, shots)
        return {
            "status": "candidate",
            "holder_candidate": player,
            "ball": {"track_id": frame.ball_id},
            "review_required": True,
        }

    def steady(self) -> bool:
        """Reject a ball sweeping through a body rather than staying beside it."""
        return all(
            max(p["relative"][axis] for p in self.streak)
            - min(p["relative"][axis] for p in self.streak)
            <= limit
            for axis, limit in enumerate((MAX_RELATIVE_X_SPREAD, MAX_RELATIVE_Y_SPREAD))
        )

    def accept(self, player: dict, frame: BallFrame, shots: list[dict]) -> None:
        """Refresh a holder; emit only supported changes, never a teammate pass."""
        ball_id, segment, timestamp = frame.ball_id, frame.segment, frame.timestamp
        previous = self.holder
        self.holder = {"player": deepcopy(player), "last_seen": timestamp}
        if previous is None or player["team"] not in TEAMS:
            return
        before = previous["player"]
        same_player = before["track_id"] == player["track_id"]
        shot = next(
            (
                s
                for s in reversed(shots)
                if s["ball"]["track_id"] == ball_id
                and s["segment"] == segment
                and previous["last_seen"] <= s["time_seconds"] <= timestamp
                and timestamp - s["time_seconds"] <= SHOT_CONTEXT_SECONDS
            ),
            None,
        )
        separated = self.streak[0]["time"] - previous["last_seen"] >= CONTROL_SECONDS
        if same_player and not (shot and separated):
            return
        if not shot and (
            before["team"] not in TEAMS or before["team"] == player["team"]
        ):
            return
        if len(self.events) >= MAX_EVENTS:
            self.truncated = True
            return
        # Without a released shooter, the last sustained holder before the shot
        # still identifies the attacking team, but not the shooting player.
        from_team = (shot.get("team") if shot else None) or before["team"]
        recovery = recovery_type(shot, from_team, player["team"]) if shot else None
        self.events.append({
            "id": f"s{segment}-possession-{len(self.events) + 1}",
            "kind": "ball_recovery_candidate"
            if shot
            else "possession_change_candidate",
            "segment": segment,
            "time_seconds": round(self.streak[0]["time"], 6),
            "start_time_seconds": round(previous["last_seen"], 6),
            "end_time_seconds": round(timestamp, 6),
            "ball": {"track_id": ball_id},
            "from_team": from_team,
            "to_team": player["team"],
            "previous_holder_candidate": deepcopy(before),
            "loss_candidate": None if shot else deepcopy(before),
            "gain_candidate": deepcopy(player),
            "shot_event_id": shot["id"] if shot else None,
            "recovery": recovery,
            "review_required": True,
            "evidence": [
                "sustained_unique_proximity",
                "stable_relative_motion",
                recovery or "opposing_team_control",
            ],
        })
