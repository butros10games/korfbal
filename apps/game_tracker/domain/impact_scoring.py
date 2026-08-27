"""Framework-independent match-impact scoring policy."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from typing import Literal


LATEST_MATCH_IMPACT_ALGORITHM_VERSION = "v6"
MIN_SHOTS_FOR_EFFICIENCY_SCALING = 5
EFFICIENCY_RATE_VERY_GOOD = 0.5
EFFICIENCY_RATE_GOOD = 1.0 / 3.0
EFFICIENCY_RATE_FINE = 0.2

Side = Literal["home", "away"]


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
    """Return the configured shot weights, defaulting to the latest policy."""
    if version == "v1":
        return ShotImpactWeights(0.9, -0.25, -6.2, 0.55)
    if version in {"v2", "v3", "v4", "v5"}:
        return ShotImpactWeights(0.6, -0.25, -6.2, 0.8)
    if version == "v6":
        return ShotImpactWeights(0.2, -0.17, -2.94, 0.31)
    return shot_impact_weights_for_version(LATEST_MATCH_IMPACT_ALGORITHM_VERSION)


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
