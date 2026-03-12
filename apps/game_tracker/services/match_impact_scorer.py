"""Scoring helpers for match impact calculation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
import logging
import math
from operator import itemgetter
from typing import Any, Literal, TypedDict, cast

from apps.game_tracker.models import MatchData, PlayerGroup

# We intentionally reuse the match payload builders because they already encode
# the minute format/rounding used by korfbal-web graphs (e.g. "20+1").
from apps.schedule.api.match_events_payload import build_match_events, build_match_shots

from .match_impact_timeline import (
    EPS,
    TINY_X,
    RoleIntervals,
    _build_goal_switch_times,
    _compute_match_end_minutes,
    _parse_event_minutes,
    _role_at_x_with_goal_switches,
    build_match_player_role_timeline,
)


logger = logging.getLogger(__name__)


Side = Literal["home", "away"]


# Bump this when tuning the algorithm so team/season aggregations can
# opportunistically recompute persisted rows.
LATEST_MATCH_IMPACT_ALGORITHM_VERSION = "v6"


@dataclass(frozen=True)
class MatchTeamImpactFeatures:
    """Per-team sufficient statistics for fast tuning."""

    team_id: str
    goals_scored_points: float
    shooter_misses_weighted: float
    defended_shots: int
    defended_goals: int
    defended_misses: int
    doorloop_concede_points_times_defenders: float


def compute_match_team_impact_features(  # noqa: C901, PLR0912, PLR0915
    *,
    match_data: MatchData,
    algorithm_version: str = LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
) -> dict[str, MatchTeamImpactFeatures]:
    """Return per-team features used to compute impact totals."""
    match = match_data.match_link
    if not match:
        return {}

    home_team_id = str(match.home_team_id)
    away_team_id = str(match.away_team_id)

    events = build_match_events(match_data)
    shots = build_match_shots(match_data)

    match_end_minutes = _compute_match_end_minutes(events=events, shots=shots)
    goal_switch_times = _build_goal_switch_times(events)

    groups = list(
        PlayerGroup.objects
        .select_related("starting_type", "team")
        .prefetch_related("players")
        .filter(match_data=match_data)
    )

    player_team_id = _build_player_team_map(groups=groups, shots=shots, events=events)
    known_player_ids = sorted(player_team_id.keys())

    role_intervals_by_id = build_match_player_role_timeline(
        known_player_ids=known_player_ids,
        groups=groups,
        events=events,
        match_end_minutes=match_end_minutes,
    )

    side_player_ids = _build_side_player_ids(
        known_player_ids=known_player_ids,
        player_team_id=player_team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    defenders_at_x = _make_defenders_at_x(
        side_player_ids=side_player_ids,
        role_intervals_by_id=role_intervals_by_id,
        goal_switch_times=goal_switch_times,
    )

    base: dict[str, dict[str, float | int]] = {
        home_team_id: {
            "goals_scored_points": 0.0,
            "shooter_misses_weighted": 0.0,
            "defended_shots": 0,
            "defended_goals": 0,
            "defended_misses": 0,
            "doorloop_concede_points_times_defenders": 0.0,
        },
        away_team_id: {
            "goals_scored_points": 0.0,
            "shooter_misses_weighted": 0.0,
            "defended_shots": 0,
            "defended_goals": 0,
            "defended_misses": 0,
            "doorloop_concede_points_times_defenders": 0.0,
        },
    }

    goal_mult_by_player, miss_mult_by_player = _compute_shooting_efficiency_multipliers(
        shots=shots,
        algorithm_version=algorithm_version,
    )

    for x, shot in _iter_shot_events(shots):
        for_team = bool(shot.get("for_team", True))
        scored = bool(shot.get("scored"))

        shooting_team_id = _shooting_team_id_from_shot(
            shot_team_id=str(shot.get("team_id") or "").strip() or None,
            for_team=for_team,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )

        if not scored:
            shooter_id = str(shot.get("player_id") or "").strip()
            if shooter_id:
                shooter_team_id = player_team_id.get(shooter_id)
                if shooter_team_id in base:
                    base[shooter_team_id]["shooter_misses_weighted"] = float(
                        base[shooter_team_id]["shooter_misses_weighted"]
                    ) + float(miss_mult_by_player.get(shooter_id, 1.0))

        defending_side = _defending_side_for_shot(
            shot_team_id=shooting_team_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        if not defending_side:
            continue

        defending_team_id = home_team_id if defending_side == "home" else away_team_id
        defenders = defenders_at_x(defending_side, x)
        if not defenders:
            continue

        base[defending_team_id]["defended_shots"] = (
            cast(int, base[defending_team_id]["defended_shots"]) + 1
        )
        if scored:
            base[defending_team_id]["defended_goals"] = (
                cast(int, base[defending_team_id]["defended_goals"]) + 1
            )
        else:
            base[defending_team_id]["defended_misses"] = (
                cast(int, base[defending_team_id]["defended_misses"]) + 1
            )

    goal_events = _iter_goal_events(events)
    last_team_id: str | None = None
    streak = 0
    last_goal_x = 0.0

    for index, goal in enumerate(goal_events):
        for_team = bool(goal.get("for_team", True))
        scoring_team_id = _scoring_team_id_from_goal(
            goal_team_id=str(goal.get("team_id") or "").strip() or None,
            for_team=for_team,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        last_team_id, streak = _next_streak_state(
            scoring_team_id=scoring_team_id,
            last_team_id=last_team_id,
            streak=streak,
        )

        x, last_goal_x = _goal_x_for_event(
            goal=goal,
            index=index,
            last_goal_x=last_goal_x,
        )

        goal_points = _compute_goal_points(
            goal_type=str(goal.get("goal_type") or ""),
            streak=streak,
        )

        scorer_id = str(goal.get("player_id") or "").strip() if for_team else ""
        if scorer_id:
            goal_points *= float(goal_mult_by_player.get(scorer_id, 1.0))

        if scorer_id:
            scorer_team_id = player_team_id.get(scorer_id)
            if scorer_team_id in base:
                base[scorer_team_id]["goals_scored_points"] = float(
                    base[scorer_team_id]["goals_scored_points"]
                ) + float(goal_points)

        is_doorloop = "doorloop" in _normalise_goal_type(
            str(goal.get("goal_type") or "")
        )
        if not is_doorloop:
            continue

        conceding_side = _conceding_side_for_goal(
            scoring_team_id=scoring_team_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        if not conceding_side:
            continue
        conceding_team_id = home_team_id if conceding_side == "home" else away_team_id

        defenders = defenders_at_x(conceding_side, x)
        if not defenders:
            continue

        base[conceding_team_id]["doorloop_concede_points_times_defenders"] = float(
            base[conceding_team_id]["doorloop_concede_points_times_defenders"]
        ) + float(goal_points) * float(len(defenders))

    return {
        team_id: MatchTeamImpactFeatures(
            team_id=team_id,
            goals_scored_points=float(values["goals_scored_points"]),
            shooter_misses_weighted=float(values["shooter_misses_weighted"]),
            defended_shots=int(values["defended_shots"]),
            defended_goals=int(values["defended_goals"]),
            defended_misses=int(values["defended_misses"]),
            doorloop_concede_points_times_defenders=float(
                values["doorloop_concede_points_times_defenders"]
            ),
        )
        for team_id, values in base.items()
    }


MATCH_IMPACT_BREAKDOWN_CACHE_VERSION = 2

MIN_SHOTS_FOR_EFFICIENCY_SCALING = 5

EFFICIENCY_RATE_VERY_GOOD = 0.5
EFFICIENCY_RATE_GOOD = 1.0 / 3.0
EFFICIENCY_RATE_FINE = 0.2


@dataclass(frozen=True)
class ShotImpactWeights:
    """Weights used for shot-related impact scoring."""

    miss_for_penalty: float
    shot_against_total: float
    goal_against_total: float
    miss_against_total: float


def shot_impact_weights_for_version(version: str) -> ShotImpactWeights:
    """Return the weights for a given algorithm version."""
    if version == "v1":
        return ShotImpactWeights(0.9, -0.25, -6.2, 0.55)
    if version == "v2":
        return ShotImpactWeights(0.6, -0.25, -6.2, 0.8)
    if version in {"v3", "v4", "v5"}:
        return shot_impact_weights_for_version("v2")
    if version == "v6":
        return ShotImpactWeights(0.2, -0.17, -2.94, 0.31)
    logger.warning("Unknown match impact algorithm version: %s", version)
    return shot_impact_weights_for_version(LATEST_MATCH_IMPACT_ALGORITHM_VERSION)


@dataclass(frozen=True)
class ShootingEfficiencyMultipliers:
    """Per-shooter multipliers derived from match shooting efficiency."""

    goal_points: float
    miss_penalty: float


def _efficiency_multipliers_for_rate(
    *, goals: int, shots: int
) -> ShootingEfficiencyMultipliers:
    if shots < MIN_SHOTS_FOR_EFFICIENCY_SCALING:
        return ShootingEfficiencyMultipliers(goal_points=1.0, miss_penalty=1.0)

    rate = (goals / shots) if shots else 0.0
    if rate >= EFFICIENCY_RATE_VERY_GOOD:
        return ShootingEfficiencyMultipliers(goal_points=1.2, miss_penalty=0.7)
    if rate >= EFFICIENCY_RATE_GOOD:
        return ShootingEfficiencyMultipliers(goal_points=1.1, miss_penalty=0.85)
    if rate >= EFFICIENCY_RATE_FINE:
        return ShootingEfficiencyMultipliers(goal_points=1.0, miss_penalty=1.0)
    return ShootingEfficiencyMultipliers(goal_points=0.9, miss_penalty=1.15)


def _compute_shooting_efficiency_multipliers(
    *, shots: list[dict[str, Any]], algorithm_version: str
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute per-player multipliers for goal points and miss penalties."""
    if algorithm_version not in {"v3", "v4", "v5"}:
        return {}, {}

    ignore_defensive_rows = algorithm_version in {"v4", "v5"}
    attempts_by_player: dict[str, int] = {}
    goals_by_player: dict[str, int] = {}

    for shot in shots:
        if ignore_defensive_rows and shot.get("for_team") is False:
            continue

        shooter_id = str(shot.get("player_id") or "").strip()
        if not shooter_id:
            continue

        attempts_by_player[shooter_id] = attempts_by_player.get(shooter_id, 0) + 1
        if bool(shot.get("scored")):
            goals_by_player[shooter_id] = goals_by_player.get(shooter_id, 0) + 1

    goal_mult_by_player: dict[str, float] = {}
    miss_mult_by_player: dict[str, float] = {}
    for pid, shots_taken in attempts_by_player.items():
        goals = goals_by_player.get(pid, 0)
        multipliers = _efficiency_multipliers_for_rate(goals=goals, shots=shots_taken)
        goal_mult_by_player[pid] = multipliers.goal_points
        miss_mult_by_player[pid] = multipliers.miss_penalty

    return goal_mult_by_player, miss_mult_by_player


def _normalise_goal_type(value: str) -> str:
    return " ".join((value or "").lower().split()).strip()


def _goal_type_impact_weight(goal_type: str) -> float:
    normalised = _normalise_goal_type(goal_type)
    if "straf" in normalised:
        weight = 0.55
    elif "vrije" in normalised:
        weight = 0.65
    elif "korte" in normalised:
        weight = 1.35
    elif "doorloop" in normalised:
        weight = 1.25
    elif (
        "1/2 afstand" in normalised
        or "halve afstand" in normalised
        or "half afstand" in normalised
    ):
        weight = 1.1
    elif "afstand" in normalised:
        weight = 0.95
    else:
        weight = 1.0
    return weight


def _compute_streak_factor(streak: int) -> float:
    streak_boost = 0.12
    max_streak_for_bonus = 4
    effective_streak = min(max(1, int(streak)), max_streak_for_bonus)
    return 1 + (effective_streak - 1) * streak_boost


def _compute_goal_points(*, goal_type: str, streak: int) -> float:
    return 3.2 * _goal_type_impact_weight(goal_type) * _compute_streak_factor(streak)


def _next_streak_state(
    *, scoring_team_id: str | None, last_team_id: str | None, streak: int
) -> tuple[str | None, int]:
    if scoring_team_id and scoring_team_id == last_team_id:
        return last_team_id, streak + 1
    return scoring_team_id, 1


def _advance_score_state(
    *,
    home_score: int,
    away_score: int,
    scoring_team_id: str | None,
    home_team_id: str,
    away_team_id: str,
) -> tuple[int, int]:
    if scoring_team_id == home_team_id:
        return home_score + 1, away_score
    if scoring_team_id == away_team_id:
        return home_score, away_score + 1
    return home_score, away_score


def _defending_side_for_shot(
    *, shot_team_id: str | None, home_team_id: str, away_team_id: str
) -> Side | None:
    if not shot_team_id:
        return None
    if shot_team_id == home_team_id:
        return "away"
    if shot_team_id == away_team_id:
        return "home"
    return None


def _conceding_side_for_goal(
    *, scoring_team_id: str | None, home_team_id: str, away_team_id: str
) -> Side | None:
    if not scoring_team_id:
        return None
    if scoring_team_id == home_team_id:
        return "away"
    if scoring_team_id == away_team_id:
        return "home"
    return None


def _opponent_team_id(
    *, team_id: str | None, home_team_id: str, away_team_id: str
) -> str | None:
    if not team_id:
        return None
    if team_id == home_team_id:
        return away_team_id
    if team_id == away_team_id:
        return home_team_id
    return None


def _shooting_team_id_from_shot(
    *, shot_team_id: str | None, for_team: bool, home_team_id: str, away_team_id: str
) -> str | None:
    if not shot_team_id:
        return None
    _ = (for_team, home_team_id, away_team_id)
    return shot_team_id


def _scoring_team_id_from_goal(
    *, goal_team_id: str | None, for_team: bool, home_team_id: str, away_team_id: str
) -> str | None:
    if not goal_team_id:
        return None
    _ = (for_team, home_team_id, away_team_id)
    return goal_team_id


@dataclass(frozen=True)
class MatchImpactRow:
    """Computed persisted impact score for a single player in a match."""

    player_id: str
    team_id: str | None
    impact_score: Decimal


def _round_js_1dp(value: float) -> Decimal:
    rounded = math.floor(value * 10.0 + 0.5) / 10.0
    return Decimal(str(rounded))


def round_js_1dp(value: float) -> Decimal:
    """Round to 1 decimal like JS: `Math.round(x * 10) / 10`."""
    return _round_js_1dp(value)


class ImpactBreakdownItem(TypedDict):
    """Aggregated contribution for a single impact category."""

    points: float
    count: int


PlayerImpactBreakdown = dict[str, dict[str, ImpactBreakdownItem]]


def _add_breakdown(
    breakdown_by_player: PlayerImpactBreakdown,
    *,
    pid: str,
    category: str,
    delta: float,
) -> None:
    if not pid:
        return

    per_player = breakdown_by_player.setdefault(
        pid, cast(dict[str, ImpactBreakdownItem], {})
    )
    if category not in per_player:
        per_player[category] = cast(
            ImpactBreakdownItem,
            {"points": delta, "count": 1},
        )
        return

    per_player[category]["points"] += delta
    per_player[category]["count"] += 1


def _add_players_from_groups(
    *, groups: list[PlayerGroup], player_team_id: dict[str, str]
) -> None:
    for g in groups:
        tid = str(g.team.id_uuid)
        for p in g.players.all():
            pid = str(p.id_uuid)
            if pid and pid not in player_team_id:
                player_team_id[pid] = tid


def _add_players_from_shots(
    *, shots: list[dict[str, Any]], player_team_id: dict[str, str]
) -> None:
    for shot in shots:
        pid = str(shot.get("player_id") or "").strip()
        tid = str(shot.get("team_id") or "").strip()
        if pid and tid and pid not in player_team_id:
            player_team_id[pid] = tid


def _add_players_from_goals(
    *, events: list[dict[str, Any]], player_team_id: dict[str, str]
) -> None:
    for ev in events:
        if ev.get("type") != "goal":
            continue
        pid = str(ev.get("player_id") or "").strip()
        tid = str(ev.get("team_id") or "").strip()
        if pid and tid and pid not in player_team_id:
            player_team_id[pid] = tid


def _build_player_team_map(
    *,
    groups: list[PlayerGroup],
    shots: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, str]:
    player_team_id: dict[str, str] = {}
    _add_players_from_groups(groups=groups, player_team_id=player_team_id)
    _add_players_from_shots(shots=shots, player_team_id=player_team_id)
    _add_players_from_goals(events=events, player_team_id=player_team_id)
    return player_team_id


def _side_for_player(
    *, pid: str, player_team_id: dict[str, str], home: str, away: str
) -> Side | None:
    tid = player_team_id.get(pid)
    if not tid:
        return None
    if tid == home:
        return "home"
    if tid == away:
        return "away"
    return None


def _build_side_player_ids(
    *,
    known_player_ids: list[str],
    player_team_id: dict[str, str],
    home_team_id: str,
    away_team_id: str,
) -> dict[Side, list[str]]:
    side_player_ids: dict[Side, list[str]] = {"home": [], "away": []}
    for pid in known_player_ids:
        side = _side_for_player(
            pid=pid,
            player_team_id=player_team_id,
            home=home_team_id,
            away=away_team_id,
        )
        if side:
            side_player_ids[side].append(pid)
    return side_player_ids


def _make_defenders_at_x(
    *,
    side_player_ids: dict[Side, list[str]],
    role_intervals_by_id: dict[str, RoleIntervals],
    goal_switch_times: list[float],
) -> Callable[[Side, float], list[str]]:
    def defensive_role_x(x: float) -> float:
        return max(0.0, x - EPS)

    def defenders_at_x(side: Side, x: float) -> list[str]:
        defenders: list[str] = []
        for pid in side_player_ids[side]:
            role = _role_at_x_with_goal_switches(
                intervals=role_intervals_by_id.get(pid),
                x=defensive_role_x(x),
                switch_times=goal_switch_times,
                use_before_epsilon=False,
            )
            if role == "verdediging":
                defenders.append(pid)
        return defenders

    return defenders_at_x


def _iter_shot_events(
    shots: list[dict[str, Any]],
) -> list[tuple[float, dict[str, Any]]]:
    shot_events: list[tuple[float, dict[str, Any]]] = []
    for index, shot in enumerate(shots):
        parsed = _parse_event_minutes(str(shot.get("time") or ""))
        x = parsed if parsed is not None else float(index + 1)
        shot_events.append((x, shot))
    shot_events.sort(key=itemgetter(0))
    return shot_events


def _add_impact(
    impact_by_player: dict[str, float],
    pid: str,
    delta: float,
    *,
    breakdown_by_player: PlayerImpactBreakdown | None = None,
    category: str | None = None,
) -> None:
    if not pid:
        return
    impact_by_player[pid] = (impact_by_player.get(pid) or 0.0) + delta

    if breakdown_by_player is not None and category:
        _add_breakdown(
            breakdown_by_player,
            pid=pid,
            category=category,
            delta=delta,
        )


def _apply_shooter_miss_penalty(
    *,
    shooter_id: str,
    scored: bool,
    miss_for_penalty: float,
    impact_by_player: dict[str, float],
    breakdown_by_player: PlayerImpactBreakdown | None,
) -> None:
    if shooter_id and not scored:
        _add_impact(
            impact_by_player,
            shooter_id,
            -miss_for_penalty,
            breakdown_by_player=breakdown_by_player,
            category="shot_miss_for",
        )


@dataclass(frozen=True)
class _DefensiveShotTotals:
    shot_against_total: float
    goal_against_total: float
    miss_against_total: float


@dataclass(frozen=True)
class _ShotImpactContext:
    home_team_id: str
    away_team_id: str
    defenders_at_x: Callable[[Side, float], list[str]]
    impact_by_player: dict[str, float]
    breakdown_by_player: PlayerImpactBreakdown | None
    weights: ShotImpactWeights


@dataclass(frozen=True)
class _ShotImpactEvent:
    x: float
    shooting_team_id: str | None
    scored: bool


def _apply_defender_shot_shares(
    *,
    ctx: _ShotImpactContext,
    defenders: list[str],
    scored: bool,
    totals: _DefensiveShotTotals,
) -> None:
    defender_count = float(len(defenders))
    shot_share = totals.shot_against_total / defender_count
    result_share = (
        totals.goal_against_total if scored else totals.miss_against_total
    ) / defender_count

    result_category = "def_goal_against" if scored else "def_miss_against"

    for did in defenders:
        _add_impact(
            ctx.impact_by_player,
            did,
            shot_share,
            breakdown_by_player=ctx.breakdown_by_player,
            category="def_shot_against",
        )
        _add_impact(
            ctx.impact_by_player,
            did,
            result_share,
            breakdown_by_player=ctx.breakdown_by_player,
            category=result_category,
        )


def _apply_defensive_shot_impacts_for_event(
    *,
    ctx: _ShotImpactContext,
    ev: _ShotImpactEvent,
    totals: _DefensiveShotTotals,
) -> None:
    defending_side = _defending_side_for_shot(
        shot_team_id=ev.shooting_team_id,
        home_team_id=ctx.home_team_id,
        away_team_id=ctx.away_team_id,
    )
    if not defending_side:
        return

    defenders = ctx.defenders_at_x(defending_side, ev.x)
    if not defenders:
        return

    _apply_defender_shot_shares(
        ctx=ctx,
        defenders=defenders,
        scored=ev.scored,
        totals=totals,
    )


def _apply_shot_impacts(
    *,
    shots: list[dict[str, Any]],
    ctx: _ShotImpactContext,
    miss_multiplier_by_shooter: dict[str, float] | None = None,
) -> None:
    miss_for_penalty = ctx.weights.miss_for_penalty
    totals = _DefensiveShotTotals(
        shot_against_total=ctx.weights.shot_against_total,
        goal_against_total=ctx.weights.goal_against_total,
        miss_against_total=ctx.weights.miss_against_total,
    )

    for x, shot in _iter_shot_events(shots):
        for_team = bool(shot.get("for_team", True))
        shooter_id = str(shot.get("player_id") or "").strip()
        scored = bool(shot.get("scored"))

        shooting_team_id = _shooting_team_id_from_shot(
            shot_team_id=str(shot.get("team_id") or "").strip() or None,
            for_team=for_team,
            home_team_id=ctx.home_team_id,
            away_team_id=ctx.away_team_id,
        )

        miss_multiplier = (
            (miss_multiplier_by_shooter or {}).get(shooter_id, 1.0)
            if shooter_id
            else 1.0
        )
        _apply_shooter_miss_penalty(
            shooter_id=shooter_id,
            scored=scored,
            miss_for_penalty=miss_for_penalty * miss_multiplier,
            impact_by_player=ctx.impact_by_player,
            breakdown_by_player=ctx.breakdown_by_player,
        )

        _apply_defensive_shot_impacts_for_event(
            ctx=ctx,
            ev=_ShotImpactEvent(
                x=x,
                shooting_team_id=shooting_team_id,
                scored=scored,
            ),
            totals=totals,
        )


def _iter_goal_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "goal"]


def _goal_x_for_event(
    *, goal: dict[str, Any], index: int, last_goal_x: float
) -> tuple[float, float]:
    parsed = _parse_event_minutes(str(goal.get("time") or ""))
    x = parsed if parsed is not None else float(index + 1)
    if x < last_goal_x:
        x = last_goal_x + TINY_X
    return x, x


@dataclass(frozen=True)
class _GoalImpactContext:
    home_team_id: str
    away_team_id: str
    defenders_at_x: Callable[[Side, float], list[str]]
    impact_by_player: dict[str, float]
    breakdown_by_player: PlayerImpactBreakdown | None
    doorloop_concede_factor: float


@dataclass(frozen=True)
class _GoalImpactEvent:
    goal: dict[str, Any]
    goal_points: float
    scoring_team_id: str | None
    x: float


def _apply_scorer_goal_points(*, ctx: _GoalImpactContext, ev: _GoalImpactEvent) -> None:
    scorer_id = str(ev.goal.get("player_id") or "").strip()
    if scorer_id:
        _add_impact(
            ctx.impact_by_player,
            scorer_id,
            ev.goal_points,
            breakdown_by_player=ctx.breakdown_by_player,
            category="goal_scored",
        )


def _apply_doorloop_concede_penalty(
    *, ctx: _GoalImpactContext, ev: _GoalImpactEvent
) -> None:
    is_doorloop = "doorloop" in _normalise_goal_type(
        str(ev.goal.get("goal_type") or "")
    )
    if not is_doorloop:
        return

    conceding_side = _conceding_side_for_goal(
        scoring_team_id=ev.scoring_team_id,
        home_team_id=ctx.home_team_id,
        away_team_id=ctx.away_team_id,
    )
    if not conceding_side:
        return

    defenders = ctx.defenders_at_x(conceding_side, ev.x)
    for did in defenders:
        _add_impact(
            ctx.impact_by_player,
            did,
            -ev.goal_points * ctx.doorloop_concede_factor,
            breakdown_by_player=ctx.breakdown_by_player,
            category="doorloop_concede_penalty",
        )


def doorloop_concede_factor_for_version(version: str) -> float:
    """Return the per-defender doorloop concede penalty factor."""
    if version == "v6":
        return 0.0
    return 0.06


def _apply_goal_impacts(
    *,
    events: list[dict[str, Any]],
    ctx: _GoalImpactContext,
    goal_multiplier_by_scorer: dict[str, float] | None = None,
) -> None:
    goal_events = _iter_goal_events(events)
    home_score = 0
    away_score = 0
    last_team_id: str | None = None
    streak = 0

    last_goal_x = 0.0
    for index, goal in enumerate(goal_events):
        for_team = bool(goal.get("for_team", True))
        scoring_team_id = _scoring_team_id_from_goal(
            goal_team_id=str(goal.get("team_id") or "").strip() or None,
            for_team=for_team,
            home_team_id=ctx.home_team_id,
            away_team_id=ctx.away_team_id,
        )
        last_team_id, streak = _next_streak_state(
            scoring_team_id=scoring_team_id,
            last_team_id=last_team_id,
            streak=streak,
        )

        x, last_goal_x = _goal_x_for_event(
            goal=goal,
            index=index,
            last_goal_x=last_goal_x,
        )

        goal_points = _compute_goal_points(
            goal_type=str(goal.get("goal_type") or ""),
            streak=streak,
        )

        scorer_id = str(goal.get("player_id") or "").strip() if for_team else ""
        goal_multiplier = (
            (goal_multiplier_by_scorer or {}).get(scorer_id, 1.0) if scorer_id else 1.0
        )
        goal_points *= goal_multiplier

        ev_ctx = _GoalImpactEvent(
            goal=goal,
            goal_points=goal_points,
            scoring_team_id=scoring_team_id,
            x=x,
        )
        _apply_scorer_goal_points(ctx=ctx, ev=ev_ctx)
        _apply_doorloop_concede_penalty(ctx=ctx, ev=ev_ctx)

        home_score, away_score = _advance_score_state(
            home_score=home_score,
            away_score=away_score,
            scoring_team_id=scoring_team_id,
            home_team_id=ctx.home_team_id,
            away_team_id=ctx.away_team_id,
        )


def compute_match_impact_rows(
    *,
    match_data: MatchData,
    algorithm_version: str = LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
) -> list[MatchImpactRow]:
    """Compute match impact rows for storage/aggregation."""
    match = match_data.match_link
    if not match:
        return []

    home_team_id = str(match.home_team_id)
    away_team_id = str(match.away_team_id)

    events = build_match_events(match_data)
    shots = build_match_shots(match_data)

    match_end_minutes = _compute_match_end_minutes(events=events, shots=shots)
    goal_switch_times = _build_goal_switch_times(events)

    groups = list(
        PlayerGroup.objects
        .select_related("starting_type", "team")
        .prefetch_related("players")
        .filter(match_data=match_data)
    )

    player_team_id = _build_player_team_map(groups=groups, shots=shots, events=events)
    known_player_ids = sorted(player_team_id.keys())

    role_intervals_by_id = build_match_player_role_timeline(
        known_player_ids=known_player_ids,
        groups=groups,
        events=events,
        match_end_minutes=match_end_minutes,
    )

    side_player_ids = _build_side_player_ids(
        known_player_ids=known_player_ids,
        player_team_id=player_team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    defenders_at_x = _make_defenders_at_x(
        side_player_ids=side_player_ids,
        role_intervals_by_id=role_intervals_by_id,
        goal_switch_times=goal_switch_times,
    )

    impact_by_player: dict[str, float] = dict.fromkeys(known_player_ids, 0.0)
    weights = shot_impact_weights_for_version(algorithm_version)

    goal_mult_by_player, miss_mult_by_player = _compute_shooting_efficiency_multipliers(
        shots=shots,
        algorithm_version=algorithm_version,
    )

    shot_ctx = _ShotImpactContext(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        defenders_at_x=defenders_at_x,
        impact_by_player=impact_by_player,
        breakdown_by_player=None,
        weights=weights,
    )
    _apply_shot_impacts(
        shots=shots,
        ctx=shot_ctx,
        miss_multiplier_by_shooter=miss_mult_by_player,
    )

    goal_ctx = _GoalImpactContext(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        defenders_at_x=defenders_at_x,
        impact_by_player=impact_by_player,
        breakdown_by_player=None,
        doorloop_concede_factor=doorloop_concede_factor_for_version(algorithm_version),
    )
    _apply_goal_impacts(
        events=events,
        ctx=goal_ctx,
        goal_multiplier_by_scorer=goal_mult_by_player,
    )

    rows: list[MatchImpactRow] = []
    for pid, score in impact_by_player.items():
        team_id = player_team_id.get(pid)
        rows.append(
            MatchImpactRow(
                player_id=pid,
                team_id=team_id,
                impact_score=_round_js_1dp(score),
            )
        )

    return rows


def compute_match_impact_breakdown(
    *,
    match_data: MatchData,
    algorithm_version: str = LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
) -> tuple[list[MatchImpactRow], PlayerImpactBreakdown]:
    """Compute match impact rows and a per-player category breakdown."""
    match = match_data.match_link
    if not match:
        return [], {}

    home_team_id = str(match.home_team_id)
    away_team_id = str(match.away_team_id)

    events = build_match_events(match_data)
    shots = build_match_shots(match_data)

    match_end_minutes = _compute_match_end_minutes(events=events, shots=shots)
    goal_switch_times = _build_goal_switch_times(events)

    groups = list(
        PlayerGroup.objects
        .select_related("starting_type", "team")
        .prefetch_related("players")
        .filter(match_data=match_data)
    )

    player_team_id = _build_player_team_map(groups=groups, shots=shots, events=events)
    known_player_ids = sorted(player_team_id.keys())

    role_intervals_by_id = build_match_player_role_timeline(
        known_player_ids=known_player_ids,
        groups=groups,
        events=events,
        match_end_minutes=match_end_minutes,
    )

    side_player_ids = _build_side_player_ids(
        known_player_ids=known_player_ids,
        player_team_id=player_team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    defenders_at_x = _make_defenders_at_x(
        side_player_ids=side_player_ids,
        role_intervals_by_id=role_intervals_by_id,
        goal_switch_times=goal_switch_times,
    )

    impact_by_player: dict[str, float] = dict.fromkeys(known_player_ids, 0.0)
    breakdown_by_player: PlayerImpactBreakdown = {}
    weights = shot_impact_weights_for_version(algorithm_version)

    goal_mult_by_player, miss_mult_by_player = _compute_shooting_efficiency_multipliers(
        shots=shots,
        algorithm_version=algorithm_version,
    )

    shot_ctx = _ShotImpactContext(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        defenders_at_x=defenders_at_x,
        impact_by_player=impact_by_player,
        breakdown_by_player=breakdown_by_player,
        weights=weights,
    )
    _apply_shot_impacts(
        shots=shots,
        ctx=shot_ctx,
        miss_multiplier_by_shooter=miss_mult_by_player,
    )

    goal_ctx = _GoalImpactContext(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        defenders_at_x=defenders_at_x,
        impact_by_player=impact_by_player,
        breakdown_by_player=breakdown_by_player,
        doorloop_concede_factor=doorloop_concede_factor_for_version(algorithm_version),
    )
    _apply_goal_impacts(
        events=events,
        ctx=goal_ctx,
        goal_multiplier_by_scorer=goal_mult_by_player,
    )

    rows: list[MatchImpactRow] = []
    for pid, score in impact_by_player.items():
        team_id = player_team_id.get(pid)
        rows.append(
            MatchImpactRow(
                player_id=pid,
                team_id=team_id,
                impact_score=_round_js_1dp(score),
            )
        )

    return rows, breakdown_by_player
