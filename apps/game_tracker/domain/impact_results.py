"""Pure assembly of persisted impact rows and category explanations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from .impact_scoring import (
    MatchImpactContribution,
    aggregate_v7_contributions,
    aggregate_win_probability_added,
)


@dataclass(frozen=True)
class MatchImpactRow:
    """Computed persisted impact score for a single player in a match."""

    player_id: str
    team_id: str | None
    impact_score: Decimal
    win_probability_added: Decimal = Decimal("0.00000")


class ImpactBreakdownItem(TypedDict):
    """Aggregated contribution for a single impact category."""

    points: float
    count: int


PlayerImpactBreakdown = dict[str, dict[str, ImpactBreakdownItem]]


def _round_v7_score(value: float) -> Decimal:
    """Store enough precision for correct season aggregation; UI rounds to 1dp."""
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _round_wpa(value: float) -> Decimal:
    """Store WPA precisely enough to aggregate percentage-point changes."""
    return Decimal(str(value)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def add_breakdown(
    breakdown_by_player: PlayerImpactBreakdown,
    *,
    pid: str,
    category: str,
    delta: float,
) -> None:
    """Accumulate one player/category contribution without display rounding."""
    if not pid:
        return

    per_player = breakdown_by_player.setdefault(pid, {})
    if category not in per_player:
        per_player[category] = {"points": delta, "count": 1}
        return

    per_player[category]["points"] += delta
    per_player[category]["count"] += 1


def build_impact_results(
    contributions: Sequence[MatchImpactContribution],
    *,
    known_player_ids: Sequence[str],
    player_team_id: Mapping[str, str],
) -> tuple[list[MatchImpactRow], PlayerImpactBreakdown]:
    """Include inactive roster players and round only after aggregating events."""
    totals: dict[str, float] = dict.fromkeys(known_player_ids, 0.0)
    totals.update(aggregate_v7_contributions(contributions))
    wpa_totals = aggregate_win_probability_added(contributions)

    breakdown: PlayerImpactBreakdown = {}
    for contribution in contributions:
        add_breakdown(
            breakdown,
            pid=contribution.player_id,
            category=contribution.category,
            delta=contribution.points,
        )

    rows = [
        MatchImpactRow(
            player_id=player_id,
            team_id=player_team_id.get(player_id),
            impact_score=_round_v7_score(score),
            win_probability_added=_round_wpa(wpa_totals.get(player_id, 0.0)),
        )
        for player_id, score in totals.items()
    ]
    return rows, breakdown
