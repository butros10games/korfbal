"""Resolve a longer stationary overlap using continuously observed neighbours."""

from __future__ import annotations

from collections import Counter
import math
from typing import TYPE_CHECKING

from .clip_signals import center, distance


if TYPE_CHECKING:
    from .clip_refinement import IdentityRefiner

MAX_OCCLUSION_GAP = 4.0
MAX_SPAN_GAP = 0.24
MAX_SPANS = 32
MIN_WITNESSES = 2
MIN_TEAM_SUPPORT = 2
MAX_BODY_DISTANCE = 0.75
MIN_SAMPLES = 3
MAX_APPEARANCE = 0.32
ENDPOINT_TOLERANCE = 0.24
MIN_SIZE_RATIO = 0.65
MAX_SIZE_RATIO = 1.55


def observe(
    track: dict, obj: dict, objects: list, time: float, *, occluded: bool
) -> None:
    """Keep bounded presence intervals and the people beside each track endpoint."""
    spans = track.setdefault("presence", [])
    if spans and time - spans[-1][1] <= MAX_SPAN_GAP:
        spans[-1][1] = time
    else:
        spans.append([time, time])
        del spans[:-MAX_SPANS]
    box = obj["observed_bbox"]
    near = {
        other["track_id"]
        for other in objects
        if other is not obj
        and other["label"] == "player"
        and not other.get("identity_uncertain")
        and distance(center(box), center(other["observed_bbox"]))
        <= max(box[3], other["observed_bbox"][3]) * MAX_BODY_DISTANCE
    }
    track.setdefault("first_near", near)
    track["last_near"] = near
    track["last_occluded"] = occluded


def cost(refiner: IdentityRefiner, before: dict, after: dict) -> float:
    """Demand two persistent neighbours, matching kit and a nearby return."""
    gap = after["start"] - before["end"]
    a, b = before["last"], after["first"]
    if (
        not 0 < gap <= MAX_OCCLUSION_GAP
        or before["segment"] != after["segment"]
        or not before.get("last_occluded")
        or min(len(a), len(b)) < MIN_SAMPLES
    ):
        return math.inf
    if (
        before["end"] - a[-1]["time"] > ENDPOINT_TOLERANCE
        or b[0]["time"] - after["start"] > ENDPOINT_TOLERANCE
        or len({s["epoch"] for s in a + b}) != 1
    ):
        return math.inf
    # Only endpoint team evidence is relevant: an immature track may have had
    # an incorrect colour long before entering this particular overlap.
    before_teams = {s.get("shirt_team", s.get("team")) for s in a} - {None, "unknown"}
    after_votes = Counter(
        s.get("shirt_team", s.get("team"))
        for s in b
        if s.get("shirt_team", s.get("team")) not in {None, "unknown"}
    )
    team = next(iter(before_teams), None)
    if (
        len(before_teams) != 1
        or after_votes[team] < MIN_TEAM_SUPPORT
        or after_votes[team] < sum(n for key, n in after_votes.items() if key != team)
        or (len(after["teams"]) == 1 and team not in after["teams"])
    ):
        return math.inf
    witnesses = before.get("last_near", set()) & after.get("first_near", set())
    persistent = [
        key
        for key in witnesses - {before["id"], after["id"]}
        if key in refiner.tracks
        and any(
            start <= before["end"] and end >= after["start"]
            for start, end in refiner.tracks[key].get("presence", [])
        )
    ]
    if len(persistent) < MIN_WITNESSES:
        return math.inf
    ratio = a[-1]["height"] / max(1e-9, b[0]["height"])
    height = (a[-1]["height"] + b[0]["height"]) / 2
    spatial = distance(a[-1]["image"], b[0]["image"]) / (height * MAX_BODY_DISTANCE)
    clothing = refiner.clothing_error(a, b)
    if (
        not MIN_SIZE_RATIO < ratio < MAX_SIZE_RATIO
        or spatial > 1
        or clothing > MAX_APPEARANCE
    ):
        return math.inf
    return 0.55 * spatial + 0.45 * clothing / MAX_APPEARANCE
