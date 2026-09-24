"""Trace short weak observations backward from independently recovered bodies."""

from __future__ import annotations

from collections import Counter, defaultdict
import math
from operator import itemgetter
from typing import TYPE_CHECKING

from .clip_refinement_spans import anchor_gaps, reachable, unique_corrections
from .clip_segment_reconciliation import histories


if TYPE_CHECKING:
    from collections.abc import Callable

    from .clip_refinement import IdentityRefiner

LOOKBACK = 0.8
MAX_GAP = 0.24
MAX_ERROR = 0.4
MIN_MARGIN = 0.08
CONTINUATION_BONUS = 0.12
MIN_SUPPORT = 3
MIN_TEAM_SHARE = 0.8


def recover(
    refiner: IdentityRefiner,
    links: list,
    corrections: list,
    stopped: Callable[[], bool] | None = None,
) -> list | None:
    """Require the same mature identity on both sides of a bounded clothing gap."""
    if refiner.spans.truncated:
        return []
    groups, frames = evidence(refiner, links, corrections)
    anchors = team_anchors(groups)
    output = []
    for _identity, before, after in anchor_gaps(anchors):
        if after[0]["time"] - before[-1]["time"] < LOOKBACK:
            continue
        if stopped and stopped():
            return None
        team = {r["sample"]["shirt_team"] for r in before + after} - {"unknown"}
        if len(team) != 1:
            continue
        output.extend(trace(frames, before, after, next(iter(team)), anchors))
    return unique_corrections(output)


def evidence(refiner: IdentityRefiner, links: list, corrections: list) -> tuple:
    """Index immutable observations under their currently resolved identities."""
    runs = histories(refiner.spans.rows, links, corrections)
    groups = defaultdict(list)
    for rows in runs.values():
        for row in rows:
            name = row["canonical"]
            groups[name].append({
                **row,
                "display_id": refiner.tracks.get(name, {}).get(
                    "display_id", row["display_id"]
                ),
            })
    for rows in groups.values():
        rows.sort(key=itemgetter("time"))
    frames = defaultdict(list)
    for rows in groups.values():
        for row in rows:
            frames[row["time"]].append(row)
    return groups, frames


def team_anchors(groups: dict) -> dict:
    """Keep repeated clean shirt evidence for each established identity."""
    anchors = {}
    for key, rows in groups.items():
        votes = Counter(
            r["sample"]["shirt_team"]
            for r in rows
            if r["sample"]
            and not r["uncertain"]
            and r["sample"]["shirt_team"] != "unknown"
        )
        if not votes:
            continue
        winner, count = votes.most_common(1)[0]
        if count < MIN_SUPPORT or count / votes.total() < MIN_TEAM_SHARE:
            continue
        anchors[key] = [
            r for r in rows if r["sample"] and r["sample"]["shirt_team"] == winner
        ]
    return anchors


def trace(frames: dict, before: list, after: list, team: str, histories: dict) -> list:
    """Use later body movement; abstain on weak alternatives or contradictory shirts."""
    anchor = after[0]
    identity = anchor["canonical"]
    previous_id = anchor["id"]
    point = anchor["image"]
    time = anchor["time"]
    future = after[1]
    elapsed = future["time"] - time
    velocity = [(b - a) / elapsed for a, b in zip(point, future["image"], strict=True)]
    output = []
    times = sorted(
        (t for t in frames if max(before[-1]["time"], time - LOOKBACK) < t < time),
        reverse=True,
    )
    for stamp in times:
        dt = time - stamp
        if dt > MAX_GAP:
            break
        predicted = [v - dt * d for v, d in zip(point, velocity, strict=True)]
        ranked = []
        for row in frames[stamp]:
            if not eligible(row, anchor, before[-1], team, histories):
                continue
            error = math.dist(predicted, row["image"]) / max(
                1e-9, (row["height"] + anchor["height"]) / 2
            )
            if error <= MAX_ERROR:
                ranked.append((
                    error - (CONTINUATION_BONUS if row["id"] == previous_id else 0),
                    row,
                ))
        ranked.sort(key=itemgetter(0))
        if (
            not ranked
            or ranked[0][0] > MAX_ERROR
            or (len(ranked) > 1 and ranked[1][0] - ranked[0][0] < MIN_MARGIN)
        ):
            continue
        _, row = ranked[0]
        if any(r["canonical"] == identity and r is not row for r in frames[stamp]):
            continue
        velocity = [(a - b) / dt for a, b in zip(point, row["image"], strict=True)]
        point, time = row["image"], stamp
        previous_id = row["id"]
        if row["canonical"] != identity:
            output.append({
                "time_seconds": stamp,
                "from_track_id": row["id"],
                "to_track_id": identity,
                "display_id": anchor["display_id"],
                "team": team,
            })
    return output if len(output) >= MIN_SUPPORT else []


def eligible(row: dict, anchor: dict, before: dict, team: str, histories: dict) -> bool:
    """Protect independently supported people and reject opposing shirt evidence."""
    identity = anchor["canonical"]
    stamp = row["time"]
    if (row["segment"], row["epoch"]) != (anchor["segment"], anchor["epoch"]):
        return False
    if row["canonical"] != identity and (
        anchored(histories.get(row["canonical"], []), stamp)
        or future_team(histories.get(row["canonical"], []), stamp) not in {None, team}
    ):
        return False
    sample = row["sample"]
    if sample and sample["shirt_team"] not in {"unknown", team}:
        return False
    if row["canonical"] != identity and sample and not row["uncertain"]:
        return False
    return reachable(before, row) and reachable(anchor, row)


def anchored(rows: list, time: float) -> bool:
    """Do not steal a body whose existing identity is supported on both sides."""
    before = [
        r for r in rows if 0 < time - r["time"] <= LOOKBACK and not r["uncertain"]
    ]
    after = [r for r in rows if 0 < r["time"] - time <= LOOKBACK and not r["uncertain"]]
    return bool(before and after and len(before) + len(after) >= MIN_SUPPORT)


def future_team(rows: list, time: float) -> str | None:
    """Repeated near-future opposing shirt evidence vetoes a weak body assignment."""
    evidence = [
        r["sample"]["shirt_team"]
        for r in rows
        if 0 < r["time"] - time <= LOOKBACK and not r["uncertain"]
    ]
    if len(evidence) >= MIN_SUPPORT and len(set(evidence)) == 1:
        return evidence[0]
    return None
