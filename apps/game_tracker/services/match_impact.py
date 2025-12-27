"""Compute and persist Match-page impact scores.

This is a Python port of the korfbal-web Match page "impact" logic. The goal is
for season/team impact totals to be identical to the match view by:

- computing impact per player per match (timeline-aware)
- persisting the results in `PlayerMatchImpact`
- aggregating persisted rows for season/team views

Important:
    This code intentionally mirrors the frontend logic (including rounding) even
    when some choices look odd from a pure-statistics perspective.

"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass
from decimal import Decimal
import logging
import math
from operator import itemgetter
from typing import Any, Literal, TypedDict, cast

from django.core.cache import cache
from django.db import transaction

from apps.game_tracker.models import (
    MatchData,
    PlayerGroup,
    PlayerMatchImpact,
    PlayerMatchImpactBreakdown,
)
from apps.player.models.player import Player

# We intentionally reuse the match payload builders because they already encode
# the minute format/rounding used by korfbal-web graphs (e.g. "20+1").
from apps.schedule.api.match_events_payload import build_match_events, build_match_shots
from apps.team.models.team import Team


logger = logging.getLogger(__name__)

EPS = 0.001
TINY_X = 0.01

GroupRole = Literal["aanval", "verdediging", "reserve", "unknown"]
Side = Literal["home", "away"]


# Bump this when tuning the algorithm so team/season aggregations can
# opportunistically recompute persisted rows.
LATEST_MATCH_IMPACT_ALGORITHM_VERSION = "v6"


@dataclass(frozen=True)
class MatchTeamImpactFeatures:
    """Per-team sufficient statistics for fast tuning.

    These features allow computing team impact totals as a simple linear
    combination of weights, without rerunning the full per-player scoring for
    each candidate.
    """

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
    """Return per-team features used to compute impact totals.

    Notes:
        - Uses the same time parsing, role timeline, `for_team` semantics, and
          streak logic as the impact algorithm.
        - Intended for analytics/tuning tools; not used in the normal request
          path.

    """
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

    # Start with explicit empty rows for both teams so downstream code can
    # rely on presence.
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

    # Shot-derived features.
    for x, shot in _iter_shot_events(shots):
        for_team = bool(shot.get("for_team", True))
        scored = bool(shot.get("scored"))

        shooting_team_id = _shooting_team_id_from_shot(
            shot_team_id=str(shot.get("team_id") or "").strip() or None,
            for_team=for_team,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )

        # Shooter miss coefficient is weighted by the per-shooter efficiency
        # miss multiplier (v3+).
        if not scored:
            shooter_id = str(shot.get("player_id") or "").strip()
            if shooter_id:
                shooter_team_id = player_team_id.get(shooter_id)
                if shooter_team_id in base:
                    base[shooter_team_id]["shooter_misses_weighted"] = float(
                        base[shooter_team_id]["shooter_misses_weighted"]
                    ) + float(miss_mult_by_player.get(shooter_id, 1.0))

        # Defensive shares: count the defended shot outcome for the *defending*
        # side at the event minute.
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

    # Goal-derived features.
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

        # Apply the scorer efficiency multiplier (v3+). This multiplier is
        # derived from goals/shots and does not depend on the weights we tune.
        scorer_id = str(goal.get("player_id") or "").strip() if for_team else ""
        if scorer_id:
            goal_points *= float(goal_mult_by_player.get(scorer_id, 1.0))

        # Team totals are computed by summing per-player rows, so attribute goal
        # points to the scorer's team (not the goal event team_id).
        if scorer_id:
            scorer_team_id = player_team_id.get(scorer_id)
            if scorer_team_id in base:
                base[scorer_team_id]["goals_scored_points"] = float(
                    base[scorer_team_id]["goals_scored_points"]
                ) + float(goal_points)

        # Doorloop concede penalty coefficient: sum(goal_points * defenders_at_x)
        # for the conceding side.
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


# Cache schema version for per-match breakdowns.
# Bump this when changing breakdown output structure or when a previous bug may
# have cached incomplete/incorrect breakdown dicts.
MATCH_IMPACT_BREAKDOWN_CACHE_VERSION = 2


# Only apply shooting-efficiency reweighting when a player has a meaningful
# number of shot attempts in the match.
MIN_SHOTS_FOR_EFFICIENCY_SCALING = 5


# Shooting efficiency rate bands (goals / shots).
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


def shot_impact_weights_for_version(version: str) -> ShotImpactWeights:  # noqa: PLR0911
    """Return the weights for a given algorithm version.

    Notes:
        Unknown versions fall back to the latest weights.

    """
    if version == "v1":
        return ShotImpactWeights(
            miss_for_penalty=0.9,
            shot_against_total=-0.25,
            goal_against_total=-6.2,
            miss_against_total=0.55,
        )

    if version == "v2":
        # Tuning: missed shots were dominating totals; reduce shooter penalty and
        # increase defensive reward for forcing a miss.
        return ShotImpactWeights(
            miss_for_penalty=0.6,
            shot_against_total=-0.25,
            goal_against_total=-6.2,
            miss_against_total=0.8,
        )

    if version == "v3":
        # v3 keeps the v2 defensive tuning, but reweights shooter goal/miss
        # contributions based on shooting efficiency bands.
        return shot_impact_weights_for_version("v2")

    if version == "v4":
        # v4 keeps v3 weights but fixes interpretation of `Shot.for_team=False`
        # events (defensive stats) so conceded goals aren't counted as
        # "goals scored" for the defending player.
        return shot_impact_weights_for_version("v3")

    if version == "v5":
        # v5 keeps v4 semantics and weights, but caps the goal streak bonus so
        # long scoring runs don't explode goal impact totals.
        return shot_impact_weights_for_version("v4")

    if version == "v6":
        # v6 fixes interpretation of `Shot.for_team` for this codebase's stored
        # tracker data: `team_id` already represents the actual shooting/scoring
        # team, and `for_team` should not be used to flip team attribution.
        #
        # These weights were tuned on historical finished matches in this DB
        # using the `fit_match_impact_v6` management command.
        return ShotImpactWeights(
            miss_for_penalty=0.2,
            shot_against_total=-0.17,
            goal_against_total=-2.94,
            miss_against_total=0.31,
        )

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
    """Map goals/shots into multipliers.

    Interpretation (user-facing intent):
        - 1/2 (>= 50%) is very good
        - 1/3 (>= 33.3%) is good
        - 1/4..1/5 (>= 20%) is fine
        - below 1/5 (< 20%) becomes a real problem

    Notes:
        The match must have enough shot attempts for the player before we
        apply any efficiency-based scaling.

    """
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


@dataclass(frozen=True)
class Interval:
    """A half-open interval [start, end) in match-minutes."""

    start: float
    end: float


@dataclass
class RoleIntervals:
    """Role intervals for a single player over a match."""

    aanval: list[Interval]
    verdediging: list[Interval]
    reserve: list[Interval]
    unknown: list[Interval]


@dataclass(frozen=True)
class _SubEvent:
    x: int
    player_in_id: str
    player_out_id: str
    player_group_id: str


def _parse_event_minutes(value: str) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    base_raw, *rest = raw.split("+")
    try:
        base = float(base_raw)
    except ValueError:
        return None

    if not rest:
        return base

    try:
        extra = float(rest[0])
    except ValueError:
        return base

    return base + extra


def _normalise_goal_type(value: str) -> str:
    return " ".join((value or "").lower().split()).strip()


def _goal_type_impact_weight(goal_type: str) -> float:  # noqa: PLR0911
    normalised = _normalise_goal_type(goal_type)

    # Lower impact set pieces
    if "straf" in normalised:
        return 0.55
    if "vrije" in normalised:
        return 0.65

    # Close chances near the post are most impactful
    if "korte" in normalised:
        return 1.35
    if "doorloop" in normalised:
        return 1.25

    # Half distance is a distinct shot category and gets a small boost.
    if (
        "1/2 afstand" in normalised
        or "halve afstand" in normalised
        or "half afstand" in normalised
    ):
        return 1.1

    # Distance shots are valuable, but usually less "high percentage" than close
    if "afstand" in normalised:
        return 0.95

    return 1.0


def _compute_streak_factor(streak: int) -> float:
    # This boost is *team* streak based (consecutive goals by the same team).
    # Without a cap, long streaks can inflate goal impact to extreme values.
    # Capping keeps the UI more intuitive while still rewarding momentum.
    streak_boost = 0.12
    max_streak_for_bonus = 4
    effective_streak = min(max(1, int(streak)), max_streak_for_bonus)
    return 1 + (effective_streak - 1) * streak_boost


def _compute_goal_points(*, goal_type: str, streak: int) -> float:
    type_weight = _goal_type_impact_weight(goal_type)
    base_goal_points = 3.2
    return base_goal_points * type_weight * _compute_streak_factor(streak)


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
    """Return the opposing team id for a match side.

    Args:
        team_id: The team id to invert.
        home_team_id: Home team id.
        away_team_id: Away team id.

    Returns:
        str | None: The opposing team id, or None if `team_id` is not one of the
        match sides.

    """
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
    """Resolve the shooting team id from a shot payload.

    In this codebase's stored tracker data, `team_id` already represents the
    actual shooting team. The `for_team` flag is treated as a UI hint (home/away)
    and should not be used to flip team attribution.
    """
    if not shot_team_id:
        return None
    _ = (for_team, home_team_id, away_team_id)
    return shot_team_id


def _scoring_team_id_from_goal(
    *, goal_team_id: str | None, for_team: bool, home_team_id: str, away_team_id: str
) -> str | None:
    """Resolve the scoring team id from a goal event payload.

    In this codebase's tracker data, `team_id` already represents the scoring
    team for goal events. The `for_team` flag should not be used to flip team
    attribution.
    """
    if not goal_team_id:
        return None
    _ = (for_team, home_team_id, away_team_id)
    return goal_team_id


def _group_role_priority(role: GroupRole) -> int:
    if role in {"aanval", "verdediging"}:
        return 2
    if role == "reserve":
        return 1
    return 0


def _normalise_group_role(raw: str) -> GroupRole:
    name = (raw or "").lower().strip()
    if not name:
        return "unknown"
    if name.startswith("aanval"):
        return "aanval"
    if name.startswith("verdediging"):
        return "verdediging"
    if name.startswith("reserve"):
        return "reserve"
    return "unknown"


def _role_at_x_from_intervals(intervals: RoleIntervals | None, x: float) -> GroupRole:
    if not intervals:
        return "unknown"

    def in_interval(items: list[Interval]) -> bool:
        return any(x >= i.start and x < i.end for i in items)

    if in_interval(intervals.aanval):
        return "aanval"
    if in_interval(intervals.verdediging):
        return "verdediging"
    if in_interval(intervals.reserve):
        return "reserve"
    return "unknown"


def _is_attack_defense_swapped_at_x(
    *, switch_times: list[float], x: float, use_before_epsilon: bool = False
) -> bool:
    t = max(0.0, x - (EPS if use_before_epsilon else 0.0))
    switches = 0
    for sx in switch_times:
        if sx <= t:
            switches += 1
        else:
            break
    return switches % 2 == 1


def _swap_aanval_verdediging(role: GroupRole) -> GroupRole:
    if role == "aanval":
        return "verdediging"
    if role == "verdediging":
        return "aanval"
    return role


def _role_at_x_with_goal_switches(
    *,
    intervals: RoleIntervals | None,
    x: float,
    switch_times: list[float],
    use_before_epsilon: bool = False,
) -> GroupRole:
    base = _role_at_x_from_intervals(intervals, x)
    if base not in {"aanval", "verdediging"}:
        return base
    swapped = _is_attack_defense_swapped_at_x(
        switch_times=switch_times,
        x=x,
        use_before_epsilon=use_before_epsilon,
    )
    return _swap_aanval_verdediging(base) if swapped else base


def _compute_match_end_minutes(
    *, events: list[dict[str, Any]], shots: list[dict[str, Any]]
) -> float:
    times: list[float] = []
    for e in events:
        parsed = _parse_event_minutes(str(e.get("time") or ""))
        if parsed is not None:
            times.append(parsed)
    for s in shots:
        parsed = _parse_event_minutes(str(s.get("time") or ""))
        if parsed is not None:
            times.append(parsed)
    # Important: `max(1.0, *times)` breaks when `times` is empty because it
    # becomes `max(1.0)` and then treats the float as an iterable.
    return max([1.0, *times])


def compute_match_end_minutes(
    *, events: list[dict[str, Any]], shots: list[dict[str, Any]]
) -> float:
    """Public wrapper for match end minute computation.

    Notes:
        This intentionally mirrors the frontend's minute parsing rules.
        Keeping this public avoids leaking private helpers across modules.

    """
    return _compute_match_end_minutes(events=events, shots=shots)


def _build_goal_switch_times(events: list[dict[str, Any]]) -> list[float]:
    goals = [e for e in events if e.get("type") == "goal"]

    with_x: list[tuple[float, int]] = []
    for index, goal in enumerate(goals):
        parsed = _parse_event_minutes(str(goal.get("time") or ""))
        raw_x = parsed if parsed is not None else float(index + 1)
        with_x.append((raw_x, index))

    with_x.sort(key=itemgetter(0, 1))

    switch_times: list[float] = []
    last_x = 0.0
    for goal_count, (raw_x, _index) in enumerate(with_x, start=1):
        x = raw_x
        if x < last_x:
            x = last_x + TINY_X
        last_x = x
        if goal_count % 2 == 0:
            switch_times.append(x)

    return switch_times


def _subs_grouped_by_minute(
    events: list[dict[str, Any]],
) -> list[tuple[int, list[_SubEvent]]]:
    by_x: dict[int, list[_SubEvent]] = {}
    for e in events:
        if e.get("type") != "substitute":
            continue
        parsed = _parse_event_minutes(str(e.get("time") or ""))
        if parsed is None:
            continue

        # The frontend groups substitutions by integer minute.
        x_int = int(parsed)

        in_id = str(e.get("player_in_id") or "").strip()
        out_id = str(e.get("player_out_id") or "").strip()
        group_id = str(e.get("player_group_id") or "").strip()

        if not in_id and not out_id:
            continue

        sub = _SubEvent(
            x=x_int,
            player_in_id=in_id,
            player_out_id=out_id,
            player_group_id=group_id,
        )
        by_x.setdefault(x_int, []).append(sub)

    return sorted(by_x.items(), key=itemgetter(0))


def _group_role_by_id(groups: list[PlayerGroup]) -> dict[str, GroupRole]:
    role_by_id: dict[str, GroupRole] = {}
    for g in groups:
        role_by_id[str(g.id_uuid)] = _normalise_group_role(g.starting_type.name)
    return role_by_id


def _infer_end_roles(
    *,
    groups: list[PlayerGroup],
    group_role_by_id: dict[str, GroupRole],
    known_player_ids: list[str],
) -> dict[str, GroupRole]:
    end_role_by_player_id: dict[str, GroupRole] = {}
    for g in groups:
        role = group_role_by_id.get(str(g.id_uuid), "unknown")
        for p in g.players.all():
            pid = str(p.id_uuid).strip()
            if not pid:
                continue
            existing = end_role_by_player_id.get(pid)
            if existing is None or _group_role_priority(role) > _group_role_priority(
                existing
            ):
                end_role_by_player_id[pid] = role

    for pid in known_player_ids:
        pid_t = pid.strip()
        if not pid_t:
            continue
        end_role_by_player_id.setdefault(pid_t, "reserve")

    return end_role_by_player_id


def _infer_start_roles_from_subs(
    *,
    end_role_by_player_id: dict[str, GroupRole],
    subs_by_x: list[tuple[int, list[_SubEvent]]],
    group_role_by_id: dict[str, GroupRole],
) -> dict[str, GroupRole]:
    # Replay substitutions backwards to infer start roles.
    role_by_player_id = dict(end_role_by_player_id)
    for _x, subs in reversed(subs_by_x):
        desired_before: dict[str, GroupRole] = {}
        for sub in subs:
            role = group_role_by_id.get(sub.player_group_id, "unknown")
            if sub.player_out_id:
                desired_before[sub.player_out_id] = role
            if sub.player_in_id and sub.player_in_id not in desired_before:
                desired_before[sub.player_in_id] = "reserve"

        role_by_player_id.update(desired_before)

    return role_by_player_id


def _initialise_roles(
    *,
    known_player_ids: list[str],
    role_by_player_id: dict[str, GroupRole],
) -> tuple[dict[str, RoleIntervals], dict[str, tuple[GroupRole, float]]]:
    role_intervals_by_id: dict[str, RoleIntervals] = {}
    current_by_id: dict[str, tuple[GroupRole, float]] = {}

    for pid in known_player_ids:
        pid_t = pid.strip()
        if not pid_t:
            continue
        start_role = role_by_player_id.get(pid_t, "reserve")
        current_by_id[pid_t] = (start_role, 0.0)
        _ensure_role_intervals(role_intervals_by_id, pid_t)

    return role_intervals_by_id, current_by_id


def _apply_subs_forward(
    *,
    subs_by_x: list[tuple[int, list[_SubEvent]]],
    group_role_by_id: dict[str, GroupRole],
    role_intervals_by_id: dict[str, RoleIntervals],
    current_by_id: dict[str, tuple[GroupRole, float]],
) -> None:
    # Apply substitutions forward (outs first, then ins) per minute.
    for x, subs in subs_by_x:
        outs: list[str] = []
        ins: list[tuple[str, GroupRole]] = []
        for sub in subs:
            role = group_role_by_id.get(sub.player_group_id, "unknown")
            if sub.player_out_id:
                outs.append(sub.player_out_id)
            if sub.player_in_id:
                ins.append((sub.player_in_id, role))

        for pid in outs:
            _close_current_role(
                current_by_id=current_by_id,
                role_intervals_by_id=role_intervals_by_id,
                player_id=pid,
                end=float(x),
            )
            _open_role_at(
                current_by_id=current_by_id,
                role_intervals_by_id=role_intervals_by_id,
                player_id=pid,
                role="reserve",
                start=float(x),
            )

        for pid, role in ins:
            _close_current_role(
                current_by_id=current_by_id,
                role_intervals_by_id=role_intervals_by_id,
                player_id=pid,
                end=float(x),
            )
            _open_role_at(
                current_by_id=current_by_id,
                role_intervals_by_id=role_intervals_by_id,
                player_id=pid,
                role=role,
                start=float(x),
            )


def _close_roles_to_match_end(
    *,
    current_by_id: dict[str, tuple[GroupRole, float]],
    role_intervals_by_id: dict[str, RoleIntervals],
    match_end_minutes: float,
) -> None:
    end = max(0.0, float(match_end_minutes))
    for pid, (role, start) in list(current_by_id.items()):
        current_by_id.pop(pid, None)
        if end <= start + EPS:
            continue
        intervals = _ensure_role_intervals(role_intervals_by_id, pid)
        getattr(intervals, role).append(Interval(start=start, end=end))


def _ensure_role_intervals(
    store: dict[str, RoleIntervals],
    player_id: str,
) -> RoleIntervals:
    existing = store.get(player_id)
    if existing is not None:
        return existing
    fresh = RoleIntervals(aanval=[], verdediging=[], reserve=[], unknown=[])
    store[player_id] = fresh
    return fresh


def _close_current_role(
    *,
    current_by_id: dict[str, tuple[GroupRole, float]],
    role_intervals_by_id: dict[str, RoleIntervals],
    player_id: str,
    end: float,
) -> None:
    current = current_by_id.pop(player_id, None)
    if not current:
        return
    role, start = current
    if end <= start + EPS:
        return
    intervals = _ensure_role_intervals(role_intervals_by_id, player_id)
    getattr(intervals, role).append(Interval(start=start, end=end))


def _open_role_at(
    *,
    current_by_id: dict[str, tuple[GroupRole, float]],
    role_intervals_by_id: dict[str, RoleIntervals],
    player_id: str,
    role: GroupRole,
    start: float,
) -> None:
    if not player_id:
        return

    current = current_by_id.get(player_id)
    if current is not None:
        current_role, current_start = current
        if abs(current_start - start) < EPS and _group_role_priority(
            role
        ) <= _group_role_priority(current_role):
            return
        _close_current_role(
            current_by_id=current_by_id,
            role_intervals_by_id=role_intervals_by_id,
            player_id=player_id,
            end=start,
        )

    current_by_id[player_id] = (role, start)
    _ensure_role_intervals(role_intervals_by_id, player_id)


def build_match_player_role_timeline(
    *,
    known_player_ids: list[str],
    groups: list[PlayerGroup],
    events: list[dict[str, Any]],
    match_end_minutes: float,
) -> dict[str, RoleIntervals]:
    """Python port of `buildMatchPlayerRoleTimeline` used by korfbal-web."""
    group_role_by_id = _group_role_by_id(groups)

    # Substitutions grouped by minute.
    subs_by_x = _subs_grouped_by_minute(events)

    end_role_by_player_id = _infer_end_roles(
        groups=groups,
        group_role_by_id=group_role_by_id,
        known_player_ids=known_player_ids,
    )
    role_by_player_id = _infer_start_roles_from_subs(
        end_role_by_player_id=end_role_by_player_id,
        subs_by_x=subs_by_x,
        group_role_by_id=group_role_by_id,
    )
    role_intervals_by_id, current_by_id = _initialise_roles(
        known_player_ids=known_player_ids,
        role_by_player_id=role_by_player_id,
    )
    _apply_subs_forward(
        subs_by_x=subs_by_x,
        group_role_by_id=group_role_by_id,
        role_intervals_by_id=role_intervals_by_id,
        current_by_id=current_by_id,
    )
    _close_roles_to_match_end(
        current_by_id=current_by_id,
        role_intervals_by_id=role_intervals_by_id,
        match_end_minutes=match_end_minutes,
    )
    return role_intervals_by_id


@dataclass(frozen=True)
class MatchImpactRow:
    """Computed persisted impact score for a single player in a match."""

    player_id: str
    team_id: str | None
    impact_score: Decimal


def _round_js_1dp(value: float) -> Decimal:
    """Match TS: `Math.round(score * 10) / 10`.

    JS uses IEEE-754 doubles + `Math.round`. Python floats are the same
    representation, so we replicate the rounding rule in float-space.

    Note: `Math.round(x)` is equivalent to `floor(x + 0.5)`.
    """
    rounded = math.floor(value * 10.0 + 0.5) / 10.0
    return Decimal(str(rounded))


def round_js_1dp(value: float) -> Decimal:
    """Round to 1 decimal like JS: `Math.round(x * 10) / 10`.

    This is intentionally part of the public surface so callers/tests don't have
    to rely on private implementation details.
    """
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
            {
                "points": delta,
                "count": 1,
            },
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
        x = last_goal_x + 0.01
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
    """Compute match impact rows and a per-player category breakdown.

    This is intended for diagnostics/transparency UIs (e.g. Team page breakdown)
    and does not persist anything.
    """
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


def compute_match_impact_breakdown_cached(
    *,
    match_data: MatchData,
    algorithm_version: str = LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    timeout_seconds: int = 60 * 60 * 24,
) -> PlayerImpactBreakdown:
    """Return cached per-match breakdown (diagnostics).

    The Team-page breakdown endpoint aggregates per-match breakdowns across many
    matches. Computing a full match breakdown can be relatively expensive, so
    we cache the result per match + algorithm version.
    """
    cache_key = (
        "match-impact-breakdown:"
        f"v{MATCH_IMPACT_BREAKDOWN_CACHE_VERSION}:"
        f"{algorithm_version}:{match_data.id_uuid}"
    )

    try:
        cached = cache.get(cache_key)
    except Exception:
        cached = None

    if isinstance(cached, dict):
        return cached  # type: ignore[return-value]

    _rows, breakdown = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version=algorithm_version,
    )

    # If cache is unavailable (e.g. Redis/Valkey down), still return the
    # computed breakdown.
    with contextlib.suppress(Exception):
        cache.set(cache_key, breakdown, timeout=timeout_seconds)
    return breakdown


def persist_match_impact_rows(
    *,
    match_data: MatchData,
    algorithm_version: str = LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
) -> int:
    """Compute + upsert rows for a match.

    Returns:
        int: number of rows upserted.

    """
    rows = compute_match_impact_rows(
        match_data=match_data,
        algorithm_version=algorithm_version,
    )
    if not rows:
        return 0

    players_by_id: dict[str, Player] = {
        str(p.id_uuid): p
        for p in Player.objects.filter(id_uuid__in=[r.player_id for r in rows]).only(
            "id_uuid"
        )
    }

    team_ids = [r.team_id for r in rows if r.team_id]
    teams_by_id: dict[str, Team] = {
        str(t.id_uuid): t
        for t in Team.objects.filter(id_uuid__in=team_ids).only("id_uuid")
    }

    upserted = 0
    with transaction.atomic():
        for row in rows:
            player = players_by_id.get(row.player_id)
            if not player:
                continue

            team = teams_by_id.get(row.team_id) if row.team_id else None

            PlayerMatchImpact.objects.update_or_create(
                match_data=match_data,
                player=player,
                defaults={
                    "team": team,
                    "impact_score": row.impact_score,
                    "algorithm_version": algorithm_version,
                },
            )
            upserted += 1

    return upserted


def persist_match_impact_rows_with_breakdowns(
    *,
    match_data: MatchData,
    algorithm_version: str = LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
) -> int:
    """Compute + upsert impact rows AND per-player breakdown rows for a match.

    This is heavier than `persist_match_impact_rows` because it computes the full
    breakdown dict. Use it for background recompute jobs and one-time backfills.

    Returns:
        int: number of PlayerMatchImpact rows upserted.

    """
    rows, breakdown_by_player = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version=algorithm_version,
    )
    if not rows:
        return 0

    players_by_id: dict[str, Player] = {
        str(p.id_uuid): p
        for p in Player.objects.filter(id_uuid__in=[r.player_id for r in rows]).only(
            "id_uuid"
        )
    }

    team_ids = [r.team_id for r in rows if r.team_id]
    teams_by_id: dict[str, Team] = {
        str(t.id_uuid): t
        for t in Team.objects.filter(id_uuid__in=team_ids).only("id_uuid")
    }

    upserted = 0
    with transaction.atomic():
        for row in rows:
            player = players_by_id.get(row.player_id)
            if not player:
                continue

            team = teams_by_id.get(row.team_id) if row.team_id else None

            impact_obj, _created = PlayerMatchImpact.objects.update_or_create(
                match_data=match_data,
                player=player,
                defaults={
                    "team": team,
                    "impact_score": row.impact_score,
                    "algorithm_version": algorithm_version,
                },
            )

            per_player_breakdown = breakdown_by_player.get(row.player_id) or {}

            PlayerMatchImpactBreakdown.objects.update_or_create(
                impact=impact_obj,
                defaults={
                    "algorithm_version": algorithm_version,
                    "breakdown": per_player_breakdown,
                },
            )
            upserted += 1

    return upserted
