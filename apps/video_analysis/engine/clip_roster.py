"""Follow each player through a whole clip by their shirt number.

The online tracker and replay refinement only join identities inside one camera
section and across gaps of a few seconds. A player who runs out of a panning
view, or disappears behind a replay or close-up, returns as somebody new.

Teammates wear identical kit, so neither clothing colour nor generic image
features can tell them apart (measured on real clips: barely better than
chance). Court position cannot either once a player has been out of view for
more than a few seconds. The shirt number can: within one team it names one
player for the whole match. This pass therefore only acts on confident number
readings. Pieces of the same team with the same number become one player, shown
by that number, and an identity whose number changes is split where the tracker
swapped people. Without readings it changes nothing. Frames, events and
training labels stay unchanged; only replay identities are renamed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from itertools import pairwise
import math
from operator import itemgetter
from typing import Any


VERSION = 1
MAX_ROWS = 80_000
MIN_READINGS = 2
MIN_NUMBER_SHARE = 0.8
MIN_TEAM_VOTES = 3
MIN_TEAM_SHARE = 0.8
TEAMS = ("team_a", "team_b")
END_WINDOW = 0.6
SPRINT = 7.5
POSITION_SLACK = 1.5


class Roster:
    """Collect compact per-observation evidence, then join pieces after the run."""

    def __init__(self) -> None:
        """Rows keep times, court positions, shirt votes and number readings."""
        self.rows: list[tuple] = []
        self.truncated = False

    def observe(
        self,
        identity: str,
        time: float,
        court: list | None,
        team: str,
        number: str | None,
    ) -> None:
        """Record one observation; `number` is a confident shirt-number reading."""
        if len(self.rows) >= MAX_ROWS:
            self.truncated = True
            return
        self.rows.append((
            identity,
            time,
            tuple(court) if court is not None else None,
            team,
            number,
        ))

    def finish(
        self, report: dict, stopped: Callable[[], bool] | None = None
    ) -> dict | None:
        """Join refined identities into clip-long players; None when interrupted."""
        summary: dict[str, Any] = {"version": VERSION, "evidence": "shirt_number"}
        if report.get("status") != "completed":
            return report
        if self.truncated:
            return {**report, "roster": {**summary, "status": "insufficient_evidence"}}
        pieces = self.pieces(report)
        if stopped and stopped():
            return None
        groups: dict[tuple, list] = defaultdict(list)
        for piece in pieces:
            if piece["number"] and piece["team"] != "unknown":
                groups[piece["team"], piece["number"]].append(piece)
        roots: dict[str, str] = {}
        labels: dict[str, str] = {}
        conflicts = 0
        for (_, number), members in groups.items():
            conflicts += join(members, roots)
            labels[roots[members[0]["id"]]] = f"#{number}"
        splits = sum(p["id"] != p["canonical"] for p in pieces)
        if not roots and not splits:
            return {**report, "roster": {**summary, "status": "no_evidence"}}
        output = compose(report, pieces, roots, labels)
        output["roster"] = {
            **summary,
            "status": "completed",
            "numbered_pieces": sum(len(m) for m in groups.values()),
            "players": len(groups),
            "joins": sum(1 for piece, root in roots.items() if piece != root),
            "number_splits": splits,
            "conflicts": conflicts,
        }
        return output

    def pieces(self, report: dict) -> list[dict]:
        """Group rows under their refined identity, split where the number changes."""
        aliases = {
            link["from_track_id"]: link["to_track_id"] for link in report["links"]
        }
        scoped = {
            (link["time_seconds"], link["from_track_id"]): link
            for link in report.get("frame_links", [])
        }
        superseded = {
            (link["time_seconds"], link["superseded_track_id"])
            for link in report.get("frame_links", [])
            if link.get("superseded_track_id")
        }
        grouped: dict[str, list] = defaultdict(list)
        for row in self.rows:
            identity, time = row[0], row[1]
            if (time, identity) in superseded:
                continue
            link = scoped.get((time, identity))
            canonical = link["to_track_id"] if link else aliases.get(identity, identity)
            grouped[canonical].append(row)
        return [
            describe(key, part, index)
            for key, rows in grouped.items()
            for index, part in enumerate(split(sorted(rows, key=itemgetter(1))))
        ]


def split(rows: list) -> list[list]:
    """Cut an identity where a sustained run of another number begins.

    A single misread is ignored; the cut lies midway between the last reading
    of one number and the first reading of the next.
    """
    runs: list[list] = []
    for index, row in enumerate(rows):
        number = row[4]
        if number is None:
            continue
        if runs and runs[-1][0] == number:
            runs[-1][2] = index
            runs[-1][3] += 1
        else:
            runs.append([number, index, index, 1])
    kept: list[list] = []
    for run in (r for r in runs if r[3] >= MIN_READINGS):
        if kept and kept[-1][0] == run[0]:
            kept[-1][2] = run[2]
        else:
            kept.append(run)
    cuts = [(before[2] + after[1] + 1) // 2 for before, after in pairwise(kept)]
    return [rows[a:b] for a, b in pairwise([0, *cuts, len(rows)])]


def describe(key: str, rows: list, index: int) -> dict:
    """Summarize one piece: extent, team, number and court ends."""
    court = [(row[1], row[2]) for row in rows if row[2] is not None]
    teams = [row[3] for row in rows if row[3] in TEAMS]
    numbers = [row[4] for row in rows if row[4] is not None]
    return {
        "id": key if index == 0 else f"{key}~{index}",
        "canonical": key,
        "rows": [(row[0], row[1]) for row in rows],
        "start": rows[0][1],
        "end": rows[-1][1],
        "times": {row[1] for row in rows},
        "team": majority(teams, MIN_TEAM_VOTES, MIN_TEAM_SHARE) or "unknown",
        "number": majority(numbers, MIN_READINGS, MIN_NUMBER_SHARE),
        "first_xy": centre([xy for t, xy in court if t - court[0][0] <= END_WINDOW]),
        "last_xy": centre([xy for t, xy in court if court[-1][0] - t <= END_WINDOW]),
        "court_start": court[0][0] if court else None,
        "court_end": court[-1][0] if court else None,
    }


def majority(votes: list[str], minimum: int, share: float) -> str | None:
    """Return a clear majority among enough votes; mixed or sparse votes give None."""
    if len(votes) < minimum:
        return None
    value, count = Counter(votes).most_common(1)[0]
    return value if count >= share * len(votes) else None


def centre(points: list) -> tuple[float, float] | None:
    """Return the per-axis median of a few court points."""
    if not points:
        return None
    xs, ys = sorted(p[0] for p in points), sorted(p[1] for p in points)
    return xs[len(xs) // 2], ys[len(ys) // 2]


def reachable(before: dict, after: dict) -> bool:
    """Check that a player need not outrun a sprint between two placed pieces."""
    if before["last_xy"] is None or after["first_xy"] is None:
        return True
    gap = max(0.0, after["court_start"] - before["court_end"])
    distance = math.dist(before["last_xy"], after["first_xy"])
    return distance <= POSITION_SLACK + SPRINT * gap


def join(members: list[dict], roots: dict[str, str]) -> int:
    """Chain one team's pieces with one number; return the rejected pieces.

    Two pieces seen at the same time cannot both be that player, and neither can
    a piece that would require an impossible run; those stay on their own.
    """
    members.sort(key=itemgetter("start"))
    chain = members[:1]
    rejected = 0
    for piece in members[1:]:
        previous = max(
            (other for other in chain if other["end"] < piece["start"]),
            key=itemgetter("end"),
            default=None,
        )
        if any(piece["times"] & other["times"] for other in chain) or (
            previous is not None and not reachable(previous, piece)
        ):
            rejected += 1
            continue
        chain.append(piece)
    for piece in chain:
        roots[piece["id"]] = chain[0]["id"]
    return rejected


def compose(
    report: dict, pieces: list[dict], roots: dict[str, str], labels: dict[str, str]
) -> dict:
    """Rename refined identities to their numbered player.

    Whole identities get one alias; the later parts of a split identity are
    renamed per observation, overriding the refinement's frame-scoped links.
    """
    displays = {link["to_track_id"]: link.get("display_id") for link in report["links"]}

    def target(identity: str) -> dict:
        root = roots.get(identity, identity)
        return {"to_track_id": root, "display_id": labels.get(root, displays.get(root))}

    links = [
        {**link, **target(link["to_track_id"])}
        if link["to_track_id"] in roots
        else link
        for link in report["links"]
    ]
    frames = {
        (link["time_seconds"], link["from_track_id"]): (
            {**link, **target(link["to_track_id"])}
            if link["to_track_id"] in roots
            else link
        )
        for link in report.get("frame_links", [])
    }
    for piece in pieces:
        if piece["id"] == piece["canonical"]:
            continue
        for identity, time in piece["rows"]:
            base = frames.get((time, identity), {})
            # A split part is another person: drop the refinement's team guess.
            frames[time, identity] = {
                **{k: v for k, v in base.items() if k != "team"},
                "time_seconds": time,
                "from_track_id": identity,
                **target(piece["id"]),
            }
    linked = {link["from_track_id"] for link in links}
    # Aliases carry no team: shirt evidence stays with the observations, and
    # review proposals must not inherit a team inferred from the roster.
    links.extend(
        {"from_track_id": piece["id"], "source": "shirt_number", **target(piece["id"])}
        for piece in sorted(pieces, key=itemgetter("start"))
        if piece["id"] == piece["canonical"]
        and piece["id"] in roots
        and piece["id"] not in linked
    )
    return {**report, "links": links, "frame_links": list(frames.values())}
