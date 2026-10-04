"""Who is in live play, so review questions are about players.

The review session ranks long, uncertain or unrepresented views first. At match
scale most views the model cannot explain are not players at all: a half-time
studio, bench staff, spectators beside the court, a floor mopper. The pipeline
already knows two things about every tracklet:

- its kit tag: a team kit (``team_a``/``team_b``), or none (another colour,
  mixed votes or too few samples), which is what the studio, most staff and
  spectators and the referee get;
- where its feet stood, wherever its frames had a court reference: on the court
  (with the overlay's line margin) or beside it.

A tracklet is a live-play candidate when it wears a team kit and was not placed
off the court: when at least ``MIN_PLACED`` of its observations were placed, at
least half of them must be on the court. Without placements (close-ups, a
camera without court calibration) the kit decides alone.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .clip_replay import COURT_MARGIN


TEAMS = frozenset({"team_a", "team_b"})
LENGTH, WIDTH = 40.0, 20.0
MIN_PLACED = 3
MAX_ROWS = 400_000


def on_court(xy: Iterable[float], court: Mapping[str, Any] | None) -> bool:
    """Whether a court position lies on the court, lines included."""
    length = float((court or {}).get("length", LENGTH))
    width = float((court or {}).get("width", WIDTH))
    x, y = xy
    return (
        -COURT_MARGIN <= x <= length + COURT_MARGIN
        and -COURT_MARGIN <= y <= width + COURT_MARGIN
    )


class Placements:
    """Whether each measured player box stood on the court, per ``(time, track)``."""

    def __init__(self, limit: int = MAX_ROWS) -> None:
        """Keep at most ``limit`` placements; later ones are counted, not kept."""
        self.rows: dict[tuple[float, str], bool] = {}
        self.limit = limit
        self.dropped = 0

    def observe(
        self,
        persons: Iterable[Mapping[str, Any]],
        time: float,
        court: Mapping[str, Any] | None,
    ) -> None:
        """Record this frame's measured court positions (predictions do not count)."""
        for person in persons:
            xy = person.get("court_xy_m")
            track = person.get("track_id")
            if person.get("label") != "player" or person.get("estimated"):
                continue
            if xy is None or not track:
                continue
            if len(self.rows) >= self.limit:
                self.dropped += 1
                continue
            self.rows[round(time, 6), str(track)] = on_court(xy, court)

    def counts(self, owners: Mapping[tuple[float, str], str]) -> dict[str, tuple]:
        """Placed and on-court observations per tracklet owning the rows."""
        totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for key, inside in self.rows.items():
            identity = owners.get(key)
            if identity is not None:
                totals[identity][0] += 1
                totals[identity][1] += int(inside)
        return {identity: (placed, on) for identity, (placed, on) in totals.items()}


def in_play(team: str, placement: tuple[int, int] | None) -> bool:
    """Whether a tracklet is a live-play candidate; see the module docstring."""
    if team not in TEAMS:
        return False
    placed, on = placement or (0, 0)
    return placed < MIN_PLACED or 2 * on >= placed
