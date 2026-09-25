"""Bounded, review-only shot hypotheses from observed ball/basket trajectories.

Basket-relative coordinates cancel image translation and uniform zoom. They do
not resolve depth: even an above/inside/below sequence is only a possible goal.
No interpolated ball, court-projected ball, or scoreboard state supplies evidence.
The shooter candidate is the sustained holder immediately before release, never
a player who merely appears near the rising ball.
"""

from collections import deque
from collections.abc import Sequence
from copy import deepcopy
from itertools import pairwise
import math
from operator import itemgetter

from .clip_ball_chain import MAX_GAP, BallFrame


VERSION = 2
MAX_EVENTS = 64
MAX_REPLAY_EVENTS = 1024
HISTORY_SECONDS = 2.5
BASKET_SCALE_RANGE = (0.5, 2.0)
BASKET_MATCH_DISTANCE = 3
BASKET_MATCH_MARGIN = 0.75
MIN_PROJECTIVE_DEPTH = 1e-6
MIN_OBSERVATIONS = 3
MIN_RISES = 2
MIN_RISE = 1.2
MIN_APPROACH = 0.5
MAX_APPROACH_X = 1.8
MAX_APPROACH_Y = 0.25
DIRECTION_TOLERANCE = 0.1
GOAL_CORRIDOR_X = 0.4
MAX_PASSAGE_SECONDS = 0.6
END_Y = 2.5
MAX_ATTEMPT_SECONDS = 4
COOLDOWN_SECONDS = 0.6
RELEASE_SECONDS = 1.5
TEAMS = {"team_a", "team_b"}


def box(obj: dict) -> list:
    """Use detector measurements rather than smoothed display boxes."""
    return obj.get("observed_bbox", obj["bbox"])


def centre(bounds: list) -> tuple[float, float]:
    """Return the centre of a normalized detection box."""
    x, y, w, h = bounds
    return x + w / 2, y + h / 2


def target(
    baskets: list[dict],
    previous: list | None,
    ball: dict,
    motion: Sequence[Sequence[float]] | None,
) -> dict | None:
    """Match one basket, rejecting jumps and ambiguous alternatives."""
    point = centre(previous or box(ball))
    if previous and motion is not None:
        x, y = point
        q = [sum(row[i] * v for i, v in enumerate((x, y, 1))) for row in motion]
        if abs(q[2]) < MIN_PROJECTIVE_DEPTH:
            return None
        point = q[0] / q[2], q[1] / q[2]
    ranked = []
    for basket in baskets:
        bounds = box(basket)
        if min(bounds[2:]) <= 0:
            continue
        if previous and not all(
            BASKET_SCALE_RANGE[0] <= bounds[i] / previous[i] <= BASKET_SCALE_RANGE[1]
            for i in (2, 3)
        ):
            continue
        x, y = centre(bounds)
        score = math.hypot((x - point[0]) / bounds[2], (y - point[1]) / bounds[3])
        ranked.append((score, basket))
    ranked.sort(key=itemgetter(0))
    if not ranked or (previous and ranked[0][0] > BASKET_MATCH_DISTANCE):
        return None
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < BASKET_MATCH_MARGIN:
        return None
    return ranked[0][1]


class ShotEvents:
    """Turn one ball chain's basket-relative arc into inspectable shot candidates."""

    def __init__(self) -> None:
        """Keep trajectories and published candidates under fixed memory limits."""
        self.events: list[dict] = []
        self.history: deque[dict] = deque(maxlen=60)
        self.pending: dict | None = None
        self.basket: list | None = None
        self.segment: int | None = None
        self.last_time: float | None = None
        self.cooldown = -math.inf
        self.truncated = False

    def finish(self, reason: str = "clip_ended") -> None:
        """Close incomplete evidence without turning disappearance into a miss."""
        if self.pending:
            self.pending["closed_reason"] = reason
            self.pending = None
        self.history.clear()
        self.basket = None
        self.last_time = None

    def summaries(self) -> list[dict]:
        """Return detached events; frame chunks already retain the trajectories."""
        return [
            deepcopy({k: v for k, v in event.items() if k != "trajectory"})
            for event in self.events
        ]

    def update(self, frame: BallFrame, holder: dict | None) -> None:
        """Record one chained ball measurement relative to an unambiguous basket."""
        ball, timestamp = frame.ball, frame.timestamp
        assert ball is not None
        if self.segment != frame.segment:
            self.cooldown = -math.inf
        self.segment = frame.segment
        if self.last_time is not None and not 0 < timestamp - self.last_time <= MAX_GAP:
            self.finish("observation_gap")
        baskets = [
            o
            for o in frame.objects
            if o["label"] == "basket" and not o.get("estimated")
        ]
        basket = target(baskets, self.basket, ball, frame.motion)
        if basket is None:
            # The ball chain survives; only basket-relative evidence restarts.
            self.finish("basket_unavailable")
            return
        self.last_time = timestamp
        self.basket = list(box(basket))
        bx, by, bw, bh = self.basket
        cx, cy = centre(box(ball))
        sample = {
            "time_seconds": round(timestamp, 6),
            "x": (cx - bx - bw / 2) / bw,
            "y": (cy - by) / bh,
            "radius_y": box(ball)[3] / (2 * bh),
        }
        self.history.append(sample)
        while (
            self.history
            and timestamp - self.history[0]["time_seconds"] > HISTORY_SECONDS
        ):
            self.history.popleft()
        if self.pending is None and timestamp >= self.cooldown:
            self.start(sample, frame.ball_id, holder)
        if self.pending:
            self.advance(sample)

    def start(self, sample: dict, ball_id: str | None, holder: dict | None) -> None:
        """Require sustained ascent toward the basket, not a stationary overlap."""
        if (
            len(self.history) < MIN_OBSERVATIONS
            or abs(sample["x"]) > MAX_APPROACH_X
            or sample["y"] > MAX_APPROACH_Y
        ):
            return
        past = list(self.history)
        first = past[0]
        rising = sum(b["y"] < a["y"] - DIRECTION_TOLERANCE for a, b in pairwise(past))
        if rising < MIN_RISES or first["y"] - sample["y"] < MIN_RISE:
            return
        if abs(first["x"]) > 1 and abs(first["x"]) - abs(sample["x"]) < MIN_APPROACH:
            return
        if len(self.events) >= MAX_EVENTS:
            self.truncated = True
            return
        # Only a holder whose sustained control ended shortly before the ascent
        # can be the shooter; proximity to the flying ball is not evidence.
        shooter = (
            deepcopy(holder["player"])
            if holder
            and 0 <= sample["time_seconds"] - holder["last_seen"] <= RELEASE_SECONDS
            else None
        )
        self.pending = {
            "id": f"s{self.segment}-shot-{len(self.events) + 1}",
            "kind": "shot_candidate",
            "segment": self.segment,
            "time_seconds": sample["time_seconds"],
            "start_time_seconds": first["time_seconds"],
            "end_time_seconds": sample["time_seconds"],
            "ball": {"track_id": ball_id},
            "shooter_candidate": shooter,
            "team": shooter["team"] if shooter and shooter["team"] in TEAMS else None,
            "basket_bbox": list(self.basket or []),
            "outcome": "unknown",
            "review_required": True,
            "evidence": ["rising_toward_basket"]
            + (["released_by_holder"] if shooter else []),
            "trajectory": [],
        }
        self.events.append(self.pending)

    def advance(self, sample: dict) -> None:
        """Record descent through the image of the basket as a possible goal only."""
        event = self.pending
        assert event is not None
        event["end_time_seconds"] = sample["time_seconds"]
        trajectory = event["trajectory"]
        trajectory.append({
            k: sample[k] for k in ("time_seconds", "x", "y", "radius_y")
        })
        del trajectory[:-40]
        if event["outcome"] == "unknown" and len(trajectory) >= MIN_OBSERVATIONS:
            # All three stages must be observed, with an unbroken descending
            # corridor. An airborne pass in front of the korf can still match.
            for index, above in enumerate(trajectory[:-2]):
                corridor = trajectory[index:]
                inside = any(0 <= p["y"] <= 1 for p in corridor[1:-1])
                descending = all(
                    b["y"] >= a["y"] - DIRECTION_TOLERANCE
                    for a, b in pairwise(corridor)
                )
                if not inside or not descending:
                    continue
                if (
                    above["y"] + above["radius_y"] < 0
                    and sample["y"] - sample["radius_y"] > 1
                    and sample["time_seconds"] - above["time_seconds"]
                    <= MAX_PASSAGE_SECONDS
                    and all(abs(p["x"]) <= GOAL_CORRIDOR_X for p in corridor)
                ):
                    event["outcome"] = "possible_goal"
                    event["evidence"].append("observed_above_inside_below")
                    event["time_seconds"] = next(
                        p["time_seconds"] for p in corridor if p["y"] >= 0
                    )
                    break
        if (
            sample["y"] > END_Y
            or sample["time_seconds"] - event["start_time_seconds"]
            > MAX_ATTEMPT_SECONDS
        ):
            self.cooldown = sample["time_seconds"] + COOLDOWN_SECONDS
            self.finish("trajectory_ended")
