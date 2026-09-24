"""Confirm opening shirt observations before the first immutable chunk is saved."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from .clip_clothing import (
    MIN_DOMINANCE,
    MIN_PIXELS,
    MIN_SUPPORT,
    votes as pixel_votes,
)
from .clip_replay import top_down
from .clip_signals import MIN_TEAM_VOTES, TEAM_HOLD_SECONDS


if TYPE_CHECKING:
    from numpy.typing import NDArray

    from .clip_signals import Teams


OPENING_SECONDS = 1.2
MAX_FRAMES = 24
MIN_SPAN = 0.15


class OpeningTeams:
    """Keep only a bounded opening's colour descriptors, never source images."""

    def __init__(self) -> None:
        """Start collecting until the first frame chunk is published."""
        self.samples: dict[tuple, NDArray[Any] | None] = {}
        self.continuity: dict[tuple, NDArray[Any] | None] = {}
        self.frames = 0
        self.start: float | None = None
        self.closed = False
        self.pixels: dict[tuple, NDArray[Any]] = {}

    def observe(self, teams: Teams, objects: list, time: float, segment: int) -> None:
        """Retain own-shirt evidence; contested identities cannot seed confirmation."""
        if self.start is None:
            self.start = time
        if (
            self.closed
            or self.frames >= MAX_FRAMES
            or time - self.start > OPENING_SECONDS + 1e-6
        ):
            return
        self.frames += 1
        for obj in objects:
            if obj["label"] == "player" and not obj.get("estimated"):
                key = segment, round(time, 6), obj["track_id"]
                self.continuity[key] = teams.observations.get(obj["track_id"])
            if (
                obj["label"] == "player"
                and not obj.get("estimated")
                and not obj.get("identity_uncertain")
                and obj.get("identity_status") != "pending"
            ):
                key = segment, round(time, 6), obj["track_id"]
                self.samples[key] = teams.observations.get(obj["track_id"])
                if obj["track_id"] in teams.observation_pixels:
                    self.pixels[key] = teams.observation_pixels[obj["track_id"]]

    def waiting(self, frames: list) -> bool:
        """Delay only the opening frames; progress receipts continue normally."""
        return bool(
            not self.closed
            and self.samples
            and frames
            and len(frames) < MAX_FRAMES
            and frames[-1]["time_seconds"] - frames[0]["time_seconds"]
            < OPENING_SECONDS - 1e-6
            and not any(frame.get("camera_cut") for frame in frames[1:])
        )

    def finish(
        self, frames: list, teams: Teams, court: dict | None, *, confirm: bool
    ) -> int:
        """Fill compatible unknown colours in unpublished frames, then forget them."""
        if self.closed:
            return 0
        self.closed = True
        samples, self.samples = self.samples, {}
        pixels, self.pixels = self.pixels, {}
        continuity, self.continuity = self.continuity, {}
        if not confirm or not samples:
            return 0
        votes = defaultdict(list)
        classified = {}
        for key, color in samples.items():
            segment, time, identity = key
            vote = teams.vote(color)
            if vote is None:
                vote = self.pixel_vote(teams, pixels.get(key))
            classified[key] = vote
            if vote is not None:
                votes[segment, identity].append((time, *vote))
        changed = sum(
            self.refine_frame(frame, court, classified, votes) for frame in frames
        )
        history = {}
        for frame in frames:
            changed += self.hold_frame(frame, court, teams, continuity, history)
        return changed

    @staticmethod
    def refine_frame(
        frame: dict, court: dict | None, classified: dict, votes: dict
    ) -> int:
        """Require own-shirt support and repeated matches of the same identity."""
        changed = 0
        for obj in frame["objects"]:
            if obj.get("team") != "unknown":
                continue
            key = frame["segment"], frame["time_seconds"], obj["track_id"]
            own = classified.get(key)
            if own is None:
                continue
            support = [
                item
                for item in votes[frame["segment"], obj["track_id"]]
                if abs(item[0] - frame["time_seconds"]) <= OPENING_SECONDS
            ]
            # A contradictory crop is a boundary, not a reason to discard all
            # earlier clean observations of the same shirt.
            before = max(
                (s[0] for s in support if s[0] < key[1] and s[1] != own[0]),
                default=float("-inf"),
            )
            after = min(
                (s[0] for s in support if s[0] > key[1] and s[1] != own[0]),
                default=float("inf"),
            )
            support = [s for s in support if before < s[0] < after]
            if (
                len(support) < MIN_TEAM_VOTES
                or max(s[0] for s in support) - min(s[0] for s in support) < MIN_SPAN
                or any(s[1] != own[0] for s in support)
            ):
                continue
            obj.update(
                team=f"team_{'ab'[own[0]]}",
                team_score=round(min(s[2] for s in support), 3),
                team_source="shirt_confirmation",
            )
            changed += 1
        if changed:
            frame["top_down"] = top_down(
                frame["objects"], frame["active_ball"], court, frame["calibration"]
            )
        return changed

    @staticmethod
    def hold_frame(
        frame: dict, court: dict | None, teams: Teams, continuity: dict, history: dict
    ) -> int:
        """Carry confirmed opening colours forward over unsupported shirt crops."""
        changed = 0
        for obj in frame["objects"]:
            if obj["label"] != "player":
                continue
            identity = frame["segment"], obj["track_id"]
            key = frame["segment"], frame["time_seconds"], obj["track_id"]
            prior = history.get(identity)
            # Pre-palette unclassified crops can include a neighbouring shirt.
            # Only a supported visible crop can contradict an established team.
            vote = (
                teams.vote(continuity.get(key))
                if obj.get("team_evidence") == "visible"
                else None
            )
            conflict = prior and vote and f"team_{'ab'[vote[0]]}" != prior[0]
            if (
                key not in continuity
                or obj.get("identity_status") == "pending"
                or obj.get("identity_issue") == "body_change"
                or conflict
            ):
                history.pop(identity, None)
                continue
            if obj.get("team") in {"team_a", "team_b"}:
                history[identity] = (
                    obj["team"],
                    obj["team_score"],
                    frame["time_seconds"] - obj.get("team_age_seconds", 0),
                )
            elif prior and frame["time_seconds"] - prior[2] <= TEAM_HOLD_SECONDS:
                age = max(0.0, frame["time_seconds"] - prior[2])
                obj.update(
                    team=prior[0],
                    team_score=round(prior[1] * (1 - age / (TEAM_HOLD_SECONDS * 2)), 3),
                    team_source="track_history",
                    team_age_seconds=round(age, 3),
                )
                changed += 1
        if changed:
            frame["top_down"] = top_down(
                frame["objects"], frame["active_ball"], court, frame["calibration"]
            )
        return changed

    @staticmethod
    def pixel_vote(teams: Teams, values: NDArray[Any] | None) -> tuple | None:
        """Revisit ambiguous opening trim/number colours after learning the palette."""
        if teams.centers is None or values is None or len(values) < MIN_PIXELS:
            return None
        lab, masks = pixel_votes(teams, values)
        counts = [int(mask.sum()) for mask in masks]
        winner = int(teams.np.argmax(counts))
        if (
            counts[winner] < MIN_PIXELS
            or counts[winner] / len(values) < MIN_SUPPORT
            or counts[winner] / max(1, sum(counts)) < MIN_DOMINANCE
        ):
            return None
        return teams.vote(teams.np.median(lab[masks[winner]], axis=0))
