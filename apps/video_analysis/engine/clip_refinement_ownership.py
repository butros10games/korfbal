"""Resolve local observation ownership after two-sided identity recovery."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from itertools import pairwise
import math
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable

MIN_CONFIDENCE_MARGIN = 0.2
MAX_FRAGMENT_AREA_RATIO = 0.75
MIN_CONTAINMENT = 0.8
MIN_SHARED_AREA = 0.25
MAX_BRIDGE_SECONDS = 0.24
MAX_BRIDGE_ERROR = 0.12
MIN_BRIDGE_MARGIN = 0.06
MIN_BRIDGE_CONFIDENCE = 0.7
MIN_SIZE_RATIO = 0.65
MAX_SIZE_RATIO = 1.55


def coverage(box: list, other: list) -> float:
    """Return the fraction of one observed box covered by another."""
    x, y, w, h = box
    a, b, c, d = other
    overlap = max(0, min(x + w, a + c) - max(x, a)) * max(
        0, min(y + h, b + d) - max(y, b)
    )
    return overlap / max(1e-9, w * h)


def supersedes(row: dict, rival: dict, frame: list[dict]) -> bool:
    """Replace only an occluded, weak fragment embedded in another body.

    The caller must independently establish the replacement's identity from
    clear observations on both sides. Never use this to resolve two clear people.
    """
    box, other = row.get("box"), rival.get("box")
    if box is None or other is None or rival["sample"] is not None:
        return False
    if (
        row.get("confidence", 0) - rival.get("confidence", 0) < MIN_CONFIDENCE_MARGIN
        or other[2] * other[3] >= MAX_FRAGMENT_AREA_RATIO * box[2] * box[3]
        or coverage(other, box) < MIN_SHARED_AREA
    ):
        return False
    return any(
        third["id"] not in {row["id"], rival["id"]}
        and third.get("confidence", 0) - rival.get("confidence", 0)
        >= MIN_CONFIDENCE_MARGIN
        and third.get("box") is not None
        and coverage(other, third["box"]) >= MIN_CONTAINMENT
        for third in frame
    )


def bridge_cost(row: dict, before: dict, after: dict) -> float:
    """Compare a real detection to a short, camera-compensated bracket."""
    if row.get("confidence", 0) < MIN_BRIDGE_CONFIDENCE:
        return math.inf
    if len({(r["segment"], r["epoch"]) for r in (row, before, after)}) != 1:
        return math.inf
    fraction = (row["time"] - before["time"]) / (after["time"] - before["time"])
    expected = [
        a + (b - a) * fraction
        for a, b in zip(before["image"], after["image"], strict=True)
    ]
    height = before["height"] + (after["height"] - before["height"]) * fraction
    ratio = row["height"] / max(1e-9, height)
    if not MIN_SIZE_RATIO < ratio < MAX_SIZE_RATIO:
        return math.inf
    return math.dist(row["image"], expected) / max(1e-9, height)


def close_gaps(
    rows: list[dict],
    aliases: list[dict],
    corrections: list[dict],
    stopped: Callable[[], bool] | None,
) -> list[dict] | None:
    """Fill only tiny gaps between already recovered observed identities."""
    links = {link["from_track_id"]: link for link in aliases}
    scoped = {(c["time_seconds"], c["from_track_id"]): c for c in corrections}
    provisional = {c["from_track_id"] for c in corrections} - {
        c["to_track_id"] for c in corrections + aliases
    }
    suppressed = {
        (c["time_seconds"], c["superseded_track_id"])
        for c in corrections
        if c.get("superseded_track_id")
    }
    frames, histories = resolved_histories(rows, links, scoped, suppressed)
    times = sorted(frames)
    output = []
    for identity, history in histories.items():
        for index, (before, after) in enumerate(pairwise(history)):
            if stopped and stopped():
                return None
            if not supported_gap(history, index):
                continue
            for time in times[
                bisect_right(times, before["time"]) : bisect_left(times, after["time"])
            ]:
                frame = frames[time]
                if any(r["owner"] == identity for r in frame):
                    continue
                ranked = sorted(
                    (bridge_cost(r, before, after), r["id"])
                    for r in frame
                    if r["id"] in provisional and (time, r["id"]) not in scoped
                )
                if (
                    not ranked
                    or ranked[0][0] > MAX_BRIDGE_ERROR
                    or (
                        len(ranked) > 1
                        and ranked[1][0] - ranked[0][0] < MIN_BRIDGE_MARGIN
                    )
                ):
                    continue
                output.append({
                    "time_seconds": time,
                    "from_track_id": ranked[0][1],
                    "to_track_id": identity,
                    "display_id": before["display_id"],
                    "team": before["team"],
                })
    return unique_sources(output)


def unique_sources(corrections: list[dict]) -> list[dict]:
    """Reject detections claimed by two identities at once."""
    counts: dict[tuple, int] = defaultdict(int)
    for correction in corrections:
        counts[correction["time_seconds"], correction["from_track_id"]] += 1
    return [
        c for c in corrections if counts[c["time_seconds"], c["from_track_id"]] == 1
    ]


def supported_gap(history: list[dict], index: int) -> bool:
    """Require adjacent support on both sides, in one camera and team context."""
    if index == 0 or index + 2 >= len(history):
        return False
    anchors = history[index - 1 : index + 3]
    if len({(r["segment"], r["epoch"]) for r in anchors}) != 1:
        return False
    teams = {r["team"] for r in anchors} - {"unknown"}
    return len(teams) <= 1 and all(
        0 < b["time"] - a["time"] <= MAX_BRIDGE_SECONDS + 1e-6
        for a, b in pairwise(anchors)
    )


def superseded_ids(objects: list[dict], links: dict, aliases: dict) -> set[str]:
    """Suppress a fragment only while its same-identity replacement is present."""
    observed = {obj["track_id"] for obj in objects}
    return {
        link["superseded_track_id"]
        for link in links.values()
        if link.get("superseded_track_id") in observed
        and link["from_track_id"] in observed
        and link["from_track_id"] != link["superseded_track_id"]
        and aliases.get(link["superseded_track_id"], {}).get(
            "to_track_id", link["superseded_track_id"]
        )
        == link["to_track_id"]
    }


def resolved_histories(
    rows: list[dict], links: dict, scoped: dict, suppressed: set
) -> tuple[dict, dict]:
    """Index immutable observations under their completed-replay owners."""
    frames: dict[float, list] = defaultdict(list)
    histories: dict[str, list] = defaultdict(list)
    for row in rows:
        if (row["time"], row["id"]) in suppressed:
            continue
        link = scoped.get((row["time"], row["id"]), links.get(row["id"], {}))
        resolved = {
            **row,
            "owner": link.get("to_track_id", row["id"]),
            "display_id": link.get("display_id", row["display_id"]),
            "team": link.get("team", (row.get("sample") or {}).get("team", "unknown")),
        }
        frames[row["time"]].append(resolved)
        if not row["uncertain"] or link:
            histories[resolved["owner"]].append(resolved)
    return frames, histories
