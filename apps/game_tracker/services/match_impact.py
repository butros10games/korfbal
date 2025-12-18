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
from dataclasses import dataclass
from decimal import Decimal
import logging
import math
from operator import itemgetter
from typing import Any, Literal

from django.db import transaction

from apps.game_tracker.models import MatchData, PlayerGroup, PlayerMatchImpact
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
    streak_boost = 0.12
    return 1 + (streak - 1) * streak_boost


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
    return max(1.0, *times)


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


def _add_impact(impact_by_player: dict[str, float], pid: str, delta: float) -> None:
    if not pid:
        return
    impact_by_player[pid] = (impact_by_player.get(pid) or 0.0) + delta


def _apply_shot_impacts(
    *,
    shots: list[dict[str, Any]],
    home_team_id: str,
    away_team_id: str,
    defenders_at_x: Callable[[Side, float], list[str]],
    impact_by_player: dict[str, float],
) -> None:
    miss_for_penalty = 0.9
    shot_against_total = -0.25
    goal_against_total = -6.2
    miss_against_total = 0.55

    for x, shot in _iter_shot_events(shots):
        shooter_id = str(shot.get("player_id") or "").strip()
        scored = bool(shot.get("scored"))
        if shooter_id and not scored:
            _add_impact(impact_by_player, shooter_id, -miss_for_penalty)

        defending_side = _defending_side_for_shot(
            shot_team_id=str(shot.get("team_id") or "").strip() or None,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        if not defending_side:
            continue

        defenders = defenders_at_x(defending_side, x)
        if not defenders:
            continue

        total = shot_against_total + (
            goal_against_total if scored else miss_against_total
        )
        share = total / float(len(defenders))
        for did in defenders:
            _add_impact(impact_by_player, did, share)


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


@dataclass(frozen=True)
class _GoalImpactEvent:
    goal: dict[str, Any]
    goal_points: float
    scoring_team_id: str | None
    x: float


def _apply_scorer_goal_points(*, ctx: _GoalImpactContext, ev: _GoalImpactEvent) -> None:
    scorer_id = str(ev.goal.get("player_id") or "").strip()
    if scorer_id:
        _add_impact(ctx.impact_by_player, scorer_id, ev.goal_points)


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
        _add_impact(ctx.impact_by_player, did, -ev.goal_points * 0.06)


def _apply_goal_impacts(
    *,
    events: list[dict[str, Any]],
    home_team_id: str,
    away_team_id: str,
    defenders_at_x: Callable[[Side, float], list[str]],
    impact_by_player: dict[str, float],
) -> None:
    ctx = _GoalImpactContext(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        defenders_at_x=defenders_at_x,
        impact_by_player=impact_by_player,
    )

    goal_events = _iter_goal_events(events)

    home_score = 0
    away_score = 0
    last_team_id: str | None = None
    streak = 0

    last_goal_x = 0.0
    for index, goal in enumerate(goal_events):
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
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )


def compute_match_impact_rows(*, match_data: MatchData) -> list[MatchImpactRow]:
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
        PlayerGroup.objects.select_related("starting_type", "team")
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

    _apply_shot_impacts(
        shots=shots,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        defenders_at_x=defenders_at_x,
        impact_by_player=impact_by_player,
    )
    _apply_goal_impacts(
        events=events,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        defenders_at_x=defenders_at_x,
        impact_by_player=impact_by_player,
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


def persist_match_impact_rows(
    *, match_data: MatchData, algorithm_version: str = "v1"
) -> int:
    """Compute + upsert rows for a match.

    Returns:
        int: number of rows upserted.

    """
    rows = compute_match_impact_rows(match_data=match_data)
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
