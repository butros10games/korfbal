"""Scoring helpers for match impact calculation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
import logging
from operator import itemgetter
from typing import Any, TypedDict, cast

from apps.game_tracker.domain.impact_scoring import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    MatchImpactContribution,
    ShotImpactWeights,
    Side,
    advance_score_state as _advance_score_state,
    aggregate_v7_contributions,
    compute_v7_contributions,
    conceding_side_for_goal as _conceding_side_for_goal,
    defending_side_for_shot as _defending_side_for_shot,
    doorloop_concede_factor_for_version,
    efficiency_multipliers_for_rate as _efficiency_multipliers_for_rate,
    goal_points as _compute_goal_points,
    next_streak_state as _next_streak_state,
    normalise_goal_type as _normalise_goal_type,
    round_js_1dp,
    shot_impact_weights_for_version,
)
from apps.game_tracker.models import MatchData, MatchEventObservation, PlayerGroup
from apps.game_tracker.services.lineup_projections import (
    starting_group_ids_by_player,
)
from apps.game_tracker.services.match_events import active_match_events

# We intentionally reuse the match payload builders because they already encode
# the minute format/rounding used by korfbal-web graphs (e.g. "20+1").
from apps.game_tracker.services.match_timeline_payload import (
    build_match_timeline_payloads,
)

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
    algorithm_version: str = "v6",
) -> dict[str, MatchTeamImpactFeatures]:
    """Return legacy v6 per-team features used by the historical fit audit."""
    match = match_data.match_link
    if not match:
        return {}

    home_team_id = str(match.home_team_id)
    away_team_id = str(match.away_team_id)

    events, shots = build_match_timeline_payloads(match_data)

    match_end_minutes = _compute_match_end_minutes(events=events, shots=shots)
    goal_switch_times = _build_goal_switch_times(events)

    groups = list(
        PlayerGroup.objects
        .select_related("starting_type", "team")
        .prefetch_related("players")
        .filter(match_data=match_data)
    )

    player_team_id = _build_player_team_map(
        groups=groups,
        shots=shots,
        events=events,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    known_player_ids = sorted(player_team_id.keys())

    role_intervals_by_id = build_match_player_role_timeline(
        known_player_ids=known_player_ids,
        groups=groups,
        events=events,
        match_end_minutes=match_end_minutes,
        starting_group_id_by_player=(starting_group_ids_by_player(match_data) or None),
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
        scored = bool(shot.get("scored"))
        shooting_team_id = str(shot.get("team_id") or "").strip() or None

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
        scoring_team_id = str(goal.get("team_id") or "").strip() or None
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
class MatchImpactRow:
    """Computed persisted impact score for a single player in a match."""

    player_id: str
    team_id: str | None
    impact_score: Decimal


class ImpactBreakdownItem(TypedDict):
    """Aggregated contribution for a single impact category."""

    points: float
    count: int


PlayerImpactBreakdown = dict[str, dict[str, ImpactBreakdownItem]]


def _round_v7_score(value: float) -> Decimal:
    """Store enough precision for correct season aggregation; UI rounds to 1dp."""
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _compute_v7_rows_and_breakdown(
    *,
    shots: list[dict[str, Any]],
    known_player_ids: list[str],
    player_team_id: dict[str, str],
) -> tuple[list[MatchImpactRow], PlayerImpactBreakdown, list[MatchImpactContribution]]:
    contributions = compute_v7_contributions(shots)
    totals: dict[str, float] = dict.fromkeys(known_player_ids, 0.0)
    totals.update(aggregate_v7_contributions(contributions))

    breakdown: PlayerImpactBreakdown = {}
    for contribution in contributions:
        _add_breakdown(
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
        )
        for player_id, score in totals.items()
    ]
    return rows, breakdown, contributions


def _observation_responsibility(payload: object) -> tuple[str, bool] | None:
    """Extract one player role from a matched or merged shot observation."""
    if not isinstance(payload, Mapping):
        return None

    reported_player_id = str(payload.get("reported_player_id") or "").strip()
    reported_role = payload.get("reported_player_role")
    if reported_player_id and reported_role in {"shooter", "defender"}:
        return reported_player_id, reported_role == "shooter"

    record = payload.get("record")
    if isinstance(record, Mapping):
        player_id = str(record.get("player_id") or "").strip()
        for_team = record.get("for_team")
        if player_id and isinstance(for_team, bool):
            return player_id, for_team

    return _observation_responsibility(payload.get("report"))


def _with_reconciled_v7_responsibilities(
    *, match_data: MatchData, shots: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add the other player role from reconciled cross-team shot reports."""
    shots_by_source_id = {
        str(shot.get("source_id") or "").strip(): shot
        for shot in shots
        if str(shot.get("source_id") or "").strip()
    }
    if not shots_by_source_id:
        return shots

    augmented = list(shots)
    seen = {
        (
            source_id,
            str(shot.get("player_id") or "").strip(),
            bool(shot.get("for_team", True)),
        )
        for source_id, shot in shots_by_source_id.items()
    }
    active_shot_event_ids = active_match_events(
        match_data,
        source_types={"shot"},
    ).values("pk")
    observations = (
        MatchEventObservation.objects
        .filter(
            event_id__in=active_shot_event_ids,
            event__source_id__in=shots_by_source_id,
            origin=MatchEventObservation.ORIGIN_MATCHED,
        )
        .select_related("event")
        .only("payload", "event__source_id")
        .order_by("recorded_at", "id_uuid")
    )
    for observation in observations:
        source_id = str(observation.event.source_id)
        base_shot = shots_by_source_id.get(source_id)
        responsibility = _observation_responsibility(observation.payload)
        if base_shot is None or responsibility is None:
            continue

        player_id, for_team = responsibility
        key = (source_id, player_id, for_team)
        if key in seen:
            continue
        seen.add(key)
        augmented.append({
            **base_shot,
            "player_id": player_id,
            "for_team": for_team,
        })

    return augmented


def compute_match_impact_contributions(
    *, match_data: MatchData
) -> list[MatchImpactContribution]:
    """Return the event-level explanation for the current v7 score."""
    _events, shots = build_match_timeline_payloads(match_data)
    shots = _with_reconciled_v7_responsibilities(
        match_data=match_data,
        shots=shots,
    )
    return compute_v7_contributions(shots)


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
    *,
    shots: list[dict[str, Any]],
    player_team_id: dict[str, str],
    home_team_id: str,
    away_team_id: str,
) -> None:
    for shot in shots:
        pid = str(shot.get("player_id") or "").strip()
        tid = str(shot.get("team_id") or "").strip()
        if not pid or not tid or pid in player_team_id:
            continue
        if shot.get("for_team") is False:
            if tid == home_team_id:
                player_team_id[pid] = away_team_id
            elif tid == away_team_id:
                player_team_id[pid] = home_team_id
            continue
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
    home_team_id: str,
    away_team_id: str,
) -> dict[str, str]:
    player_team_id: dict[str, str] = {}
    _add_players_from_groups(groups=groups, player_team_id=player_team_id)
    _add_players_from_shots(
        shots=shots,
        player_team_id=player_team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
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
        shooter_id = str(shot.get("player_id") or "").strip()
        scored = bool(shot.get("scored"))
        shooting_team_id = str(shot.get("team_id") or "").strip() or None

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
        scoring_team_id = str(goal.get("team_id") or "").strip() or None
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

    events, shots = build_match_timeline_payloads(match_data)
    if algorithm_version == "v7":
        shots = _with_reconciled_v7_responsibilities(
            match_data=match_data,
            shots=shots,
        )

    match_end_minutes = _compute_match_end_minutes(events=events, shots=shots)
    goal_switch_times = _build_goal_switch_times(events)

    groups = list(
        PlayerGroup.objects
        .select_related("starting_type", "team")
        .prefetch_related("players")
        .filter(match_data=match_data)
    )

    player_team_id = _build_player_team_map(
        groups=groups,
        shots=shots,
        events=events,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    known_player_ids = sorted(player_team_id.keys())

    if algorithm_version == "v7":
        rows, _breakdown, _contributions = _compute_v7_rows_and_breakdown(
            shots=shots,
            known_player_ids=known_player_ids,
            player_team_id=player_team_id,
        )
        return rows

    role_intervals_by_id = build_match_player_role_timeline(
        known_player_ids=known_player_ids,
        groups=groups,
        events=events,
        match_end_minutes=match_end_minutes,
        starting_group_id_by_player=(starting_group_ids_by_player(match_data) or None),
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
                impact_score=round_js_1dp(score),
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

    events, shots = build_match_timeline_payloads(match_data)
    if algorithm_version == "v7":
        shots = _with_reconciled_v7_responsibilities(
            match_data=match_data,
            shots=shots,
        )

    match_end_minutes = _compute_match_end_minutes(events=events, shots=shots)
    goal_switch_times = _build_goal_switch_times(events)

    groups = list(
        PlayerGroup.objects
        .select_related("starting_type", "team")
        .prefetch_related("players")
        .filter(match_data=match_data)
    )

    player_team_id = _build_player_team_map(
        groups=groups,
        shots=shots,
        events=events,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    known_player_ids = sorted(player_team_id.keys())

    if algorithm_version == "v7":
        rows, breakdown, _contributions = _compute_v7_rows_and_breakdown(
            shots=shots,
            known_player_ids=known_player_ids,
            player_team_id=player_team_id,
        )
        return rows, breakdown

    role_intervals_by_id = build_match_player_role_timeline(
        known_player_ids=known_player_ids,
        groups=groups,
        events=events,
        match_end_minutes=match_end_minutes,
        starting_group_id_by_player=(starting_group_ids_by_player(match_data) or None),
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
                impact_score=round_js_1dp(score),
            )
        )

    return rows, breakdown_by_player
