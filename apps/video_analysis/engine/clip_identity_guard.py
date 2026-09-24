"""Detect body changes even when the native tracker number never disappears."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .clip_identity import IdentityMemory

MIN_HISTORY = 3
MAX_COLOR_DISTANCE = 35
MIN_CONFLICT_SAMPLES = 3
MIN_CONFLICT_SECONDS = 0.15
MAX_SAMPLE_GAP = 0.32


class ContinuationGuard:
    """Freeze a mature identity during conflicting observations before releasing it."""

    def __init__(self) -> None:
        """Keep short, consecutive conflict streaks per public identity."""
        self.conflicts: dict[str, dict] = {}

    def check(
        self,
        memory: IdentityMemory,
        matched: dict,
        objects: list,
        colors: list,
        timestamp: float,
    ) -> None:
        """Freeze isolated conflicts; release persistent contradictory bodies."""
        seen = set()
        for index, identity in list(matched.items()):
            prior, color = memory.tracks[identity], colors[index]
            if color is None and identity in self.conflicts:
                state = self.conflicts[identity]
                if timestamp - state["last"] <= MAX_SAMPLE_GAP:
                    objects[index]["identity_uncertain"] = True
                    seen.add(identity)
                    continue
            if (
                len(prior["colors"]) < MIN_HISTORY
                or color is None
                or prior["color"] is None
                or memory.shirt_distance(prior["color"], color) <= MAX_COLOR_DISTANCE
            ):
                self.conflicts.pop(identity, None)
                continue
            seen.add(identity)
            obj = objects[index]
            # An overlapping crop can contain the other person's jersey. Freeze
            # its identity/team evidence, but require clear samples to release it.
            obj["identity_uncertain"] = True
            state = self.conflicts.get(identity)
            if state is None or timestamp - state["last"] > MAX_SAMPLE_GAP:
                state = {"start": timestamp, "last": timestamp, "hits": 0}
            state["last"] = timestamp
            if (
                memory.clear_torso(obj, objects)
                or obj["track_id"] in memory.visible_shirts
            ):
                state["hits"] += 1
            else:
                state.update(hits=0, start=timestamp)
            self.conflicts[identity] = state
            if (
                state["hits"] >= MIN_CONFLICT_SAMPLES
                and timestamp - state["start"] >= MIN_CONFLICT_SECONDS
            ):
                matched.pop(index)
                memory.metric_matches.discard(index)
                obj["identity_issue"] = "body_change"
                # Never treat the disputed native link as trusted on a later frame.
                prior["native"] = None
                self.conflicts.pop(identity, None)
        self.conflicts = {k: v for k, v in self.conflicts.items() if k in seen}
