"""One continuous chain of observations for the recently active match ball.

Shot and possession evidence share this chain so both detectors break at the
same cuts, gaps and identity changes. A missing basket or holder is not a break.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import math


MAX_GAP = 0.32
MAX_ACTIVE_AGE = 4


@dataclass(frozen=True)
class BallFrame:
    """One sample's chained ball, if visible, and the scene it was observed in."""

    ball: dict | None
    ball_id: str | None
    objects: list[dict]
    timestamp: float
    segment: int
    motion: Sequence[Sequence[float]] | None = None


class BallChain:
    """Follow one visible ball that the tracker recently identified as active."""

    def __init__(self) -> None:
        """Start without a ball; the first active association establishes one."""
        self.ball_id: str | None = None
        self.segment: int | None = None
        self.last_seen = -math.inf
        self.last_active = -math.inf

    def reset(self) -> None:
        """Forget the ball; a fresh active association is required to continue."""
        self.ball_id = None
        self.last_seen = self.last_active = -math.inf

    def update(
        self, objects: list[dict], active: dict, timestamp: float, camera: dict
    ) -> tuple[dict | None, str | None]:
        """Return the observed chained ball and why earlier evidence broke, if it did.

        An airborne ball keeps its chain after losing its active score, but
        active evidence expires. Hidden, estimated or spare balls never extend it.
        """
        reason = None
        if camera.get("cut") or camera["segment"] != self.segment:
            reason = "camera_cut"
        elif self.ball_id and not 0 < timestamp - self.last_seen <= MAX_GAP:
            reason = "observation_gap"
        self.segment = camera["segment"]
        active_id = (
            active.get("track_id") if active.get("status") == "observed" else None
        )
        if active_id and self.ball_id and active_id != self.ball_id:
            reason = reason or "ball_changed"
        elif (
            not active_id
            and self.ball_id
            and timestamp - self.last_active > MAX_ACTIVE_AGE
        ):
            reason = reason or "active_ball_expired"
        if reason:
            self.reset()
        if active_id:
            if self.ball_id is None:
                self.last_seen = timestamp
            self.ball_id, self.last_active = active_id, timestamp
        ball = next(
            (
                o
                for o in objects
                if o["label"] == "ball"
                and self.ball_id
                and o.get("track_id") == self.ball_id
                and not o.get("estimated")
            ),
            None,
        )
        if ball is not None:
            self.last_seen = timestamp
        return ball, reason
