"""Review-only shot, goal, loss and win candidates from one shared ball chain.

Possession runs after shot detection in each frame: a shot uses the holder
established before release, and a later control change can then be read as a
rebound or restart instead of a turnover. Neither detector creates match facts.
"""

from copy import deepcopy
from operator import itemgetter

from .clip_ball_chain import BallChain, BallFrame
from .clip_events import (
    VERSION as SHOT_VERSION,
    ShotEvents,
)
from .clip_possession import (
    VERSION as POSSESSION_VERSION,
    PossessionEvents,
)


class MatchEvents:
    """Feed shot and possession evidence from the same continuous ball."""

    def __init__(self, court: dict | None = None) -> None:
        """Share a ball chain; each detector keeps its own event bound."""
        self.chain = BallChain()
        self.shots = ShotEvents()
        self.possession = PossessionEvents(court)
        self.frames = 0

    def update(
        self, objects: list[dict], active: dict, timestamp: float, camera: dict
    ) -> dict:
        """Consume one sampled frame and return its visible control evidence."""
        self.frames += 1
        ball, broken = self.chain.update(objects, active, timestamp, camera)
        if broken:
            self.shots.finish(broken)
            self.possession.reset()
        frame = BallFrame(
            ball,
            self.chain.ball_id,
            objects,
            timestamp,
            camera["segment"],
            camera.get("motion"),
        )
        if ball is not None:
            self.shots.update(frame, self.possession.holder)
        return self.possession.update(frame, self.shots.events)

    def finish(self, reason: str = "clip_ended") -> None:
        """Close pending evidence; disappearance never becomes a miss or a loss."""
        self.shots.finish(reason)
        self.possession.reset()
        self.chain.reset()

    def snapshot(self) -> dict:
        """Publish versioned coverage markers and detached, time-ordered events."""
        truncated = self.shots.truncated or self.possession.truncated
        marker = {
            "review_only": True,
            "processed_frames": self.frames,
            "truncated": truncated,
        }
        return {
            "event_detection": {"version": SHOT_VERSION, **marker},
            "possession_detection": {"version": POSSESSION_VERSION, **marker},
            "events": sorted(
                self.shots.summaries() + deepcopy(self.possession.events),
                key=itemgetter("time_seconds"),
            ),
        }
