"""Framework-independent match-impact scoring policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import math
from typing import Any, Literal


LATEST_MATCH_IMPACT_ALGORITHM_VERSION = "v7"

# Historical tracker data does not contain an unbiased shot-type selection for
# open-play misses. Keep one deliberately conservative open-play baseline and
# only distinguish set pieces, whose type is consistently captured.
V7_OPEN_PLAY_XG = 0.18
V7_FREE_PASS_XG = 0.35
V7_PENALTY_XG = 0.75
MIN_SHOTS_FOR_EFFICIENCY_SCALING = 5
EFFICIENCY_RATE_VERY_GOOD = 0.5
EFFICIENCY_RATE_GOOD = 1.0 / 3.0
EFFICIENCY_RATE_FINE = 0.2

Side = Literal["home", "away"]
ImpactCategory = Literal[
    "offense_goal_above_expected",
    "offense_miss_below_expected",
    "defense_stop_above_expected",
    "defense_goal_below_expected",
]


@dataclass(frozen=True)
class MatchImpactContribution:
    """One auditable v7 contribution from a tracked shot."""

    player_id: str
    time: str
    category: ImpactCategory
    points: float
    expected_goals: float
    scored: bool
    for_team: bool
    shot_type: str


def expected_goal_probability(shot_type: str | None) -> float:
    """Return the v7 expected-goal baseline for a tracked shot type."""
    normalised = normalise_goal_type(shot_type or "")
    if "straf" in normalised or "penalty" in normalised:
        return V7_PENALTY_XG
    if "vrije" in normalised or "free pass" in normalised:
        return V7_FREE_PASS_XG
    return V7_OPEN_PLAY_XG


def compute_v7_contributions(
    shots: Sequence[Mapping[str, Any]],
) -> list[MatchImpactContribution]:
    """Score shots as goals above expected for the responsible player.

    ``for_team=True`` identifies the attacking player. ``for_team=False``
    identifies the directly responsible defender while ``team_id`` remains the
    shooting team. This is the tracker write contract.
    """
    contributions: list[MatchImpactContribution] = []
    for shot in shots:
        player_id = str(shot.get("player_id") or "").strip()
        if not player_id:
            continue

        shot_type = str(shot.get("shot_type") or "")
        expected = expected_goal_probability(shot_type)
        scored = bool(shot.get("scored"))
        for_team = bool(shot.get("for_team", True))

        if for_team:
            points = (1.0 - expected) if scored else -expected
            category: ImpactCategory = (
                "offense_goal_above_expected"
                if scored
                else "offense_miss_below_expected"
            )
        else:
            points = -(1.0 - expected) if scored else expected
            category = (
                "defense_goal_below_expected"
                if scored
                else "defense_stop_above_expected"
            )

        contributions.append(
            MatchImpactContribution(
                player_id=player_id,
                time=str(shot.get("time") or "?"),
                category=category,
                points=points,
                expected_goals=expected,
                scored=scored,
                for_team=for_team,
                shot_type=shot_type,
            )
        )
    return contributions


def aggregate_v7_contributions(
    contributions: list[MatchImpactContribution],
) -> dict[str, float]:
    """Aggregate v7 contributions by player without display rounding."""
    totals: dict[str, float] = {}
    for contribution in contributions:
        totals[contribution.player_id] = (
            totals.get(contribution.player_id, 0.0) + contribution.points
        )
    return totals


@dataclass(frozen=True)
class ShotImpactWeights:
    """Weights used for shot-related impact scoring."""

    miss_for_penalty: float
    shot_against_total: float
    goal_against_total: float
    miss_against_total: float


@dataclass(frozen=True)
class ShootingEfficiencyMultipliers:
    """Per-shooter multipliers derived from match shooting efficiency."""

    goal_points: float
    miss_penalty: float


def shot_impact_weights_for_version(version: str) -> ShotImpactWeights:
    """Return legacy shot weights, defaulting unknown legacy versions to v6."""
    if version == "v1":
        return ShotImpactWeights(0.9, -0.25, -6.2, 0.55)
    if version in {"v2", "v3", "v4", "v5"}:
        return ShotImpactWeights(0.6, -0.25, -6.2, 0.8)
    if version == "v6":
        return ShotImpactWeights(0.2, -0.17, -2.94, 0.31)
    return shot_impact_weights_for_version("v6")


def efficiency_multipliers_for_rate(
    *,
    goals: int,
    shots: int,
) -> ShootingEfficiencyMultipliers:
    """Return impact scaling for one player's shooting rate."""
    if shots < MIN_SHOTS_FOR_EFFICIENCY_SCALING:
        return ShootingEfficiencyMultipliers(1.0, 1.0)
    rate = (goals / shots) if shots else 0.0
    if rate >= EFFICIENCY_RATE_VERY_GOOD:
        return ShootingEfficiencyMultipliers(1.2, 0.7)
    if rate >= EFFICIENCY_RATE_GOOD:
        return ShootingEfficiencyMultipliers(1.1, 0.85)
    if rate >= EFFICIENCY_RATE_FINE:
        return ShootingEfficiencyMultipliers(1.0, 1.0)
    return ShootingEfficiencyMultipliers(0.9, 1.15)


def normalise_goal_type(value: str) -> str:
    """Normalize goal-type labels used by historical tracker data."""
    return " ".join((value or "").lower().split()).strip()


def goal_points(*, goal_type: str, streak: int) -> float:
    """Compute scorer impact for a goal type and team streak."""
    normalised = normalise_goal_type(goal_type)
    if "straf" in normalised:
        weight = 0.55
    elif "vrije" in normalised:
        weight = 0.65
    elif "korte" in normalised:
        weight = 1.35
    elif "doorloop" in normalised:
        weight = 1.25
    elif any(
        label in normalised
        for label in ("1/2 afstand", "halve afstand", "half afstand")
    ):
        weight = 1.1
    elif "afstand" in normalised:
        weight = 0.95
    else:
        weight = 1.0
    effective_streak = min(max(1, int(streak)), 4)
    streak_factor = 1 + (effective_streak - 1) * 0.12
    return 3.2 * weight * streak_factor


def next_streak_state(
    *,
    scoring_team_id: str | None,
    last_team_id: str | None,
    streak: int,
) -> tuple[str | None, int]:
    """Advance consecutive-team scoring state."""
    if scoring_team_id and scoring_team_id == last_team_id:
        return last_team_id, streak + 1
    return scoring_team_id, 1


def advance_score_state(
    *,
    home_score: int,
    away_score: int,
    scoring_team_id: str | None,
    home_team_id: str,
    away_team_id: str,
) -> tuple[int, int]:
    """Apply a goal to a home/away score pair."""
    if scoring_team_id == home_team_id:
        return home_score + 1, away_score
    if scoring_team_id == away_team_id:
        return home_score, away_score + 1
    return home_score, away_score


def opposing_side(
    *,
    team_id: str | None,
    home_team_id: str,
    away_team_id: str,
) -> Side | None:
    """Return the side opposing a participating team."""
    if team_id == home_team_id:
        return "away"
    if team_id == away_team_id:
        return "home"
    return None


def defending_side_for_shot(
    *,
    shot_team_id: str | None,
    home_team_id: str,
    away_team_id: str,
) -> Side | None:
    """Return the defending side for a shot."""
    return opposing_side(
        team_id=shot_team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )


def conceding_side_for_goal(
    *,
    scoring_team_id: str | None,
    home_team_id: str,
    away_team_id: str,
) -> Side | None:
    """Return the conceding side for a goal."""
    return opposing_side(
        team_id=scoring_team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )


def round_js_1dp(value: float) -> Decimal:
    """Round to one decimal using JavaScript ``Math.round`` semantics."""
    return Decimal(str(math.floor(value * 10.0 + 0.5) / 10.0))


def doorloop_concede_factor_for_version(version: str) -> float:
    """Return the per-defender doorloop concede penalty factor."""
    return 0.0 if version == "v6" else 0.06
