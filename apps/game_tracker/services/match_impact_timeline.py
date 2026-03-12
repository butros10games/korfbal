"""Timeline reconstruction helpers for match impact scoring."""

from __future__ import annotations

from dataclasses import dataclass
from operator import itemgetter
from typing import Any, Literal

from apps.game_tracker.models import PlayerGroup


EPS = 0.001
TINY_X = 0.01

GroupRole = Literal["aanval", "verdediging", "reserve", "unknown"]


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
    """Public wrapper for match end minute computation."""
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
