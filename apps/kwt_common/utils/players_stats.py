"""Module contains `players_stats` function that returns player stats in a match.

Team/season pages should report impact scores consistent with the Match page.

We achieve this by:
- persisting per-player per-match impact rows using the match-page algorithm
- aggregating those persisted rows for team/season totals

When persisted rows are missing or outdated, we opportunistically recompute them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
import json
import logging
import operator
from typing import Any, TypedDict, cast

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.db.models import Count, Q, QuerySet, Sum

from apps.game_tracker.models import MatchData, PlayerGroup, PlayerMatchImpact, Shot
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    build_match_player_role_timeline,
    compute_match_end_minutes,
    persist_match_impact_rows,
)
from apps.schedule.api.match_events_payload import build_match_events, build_match_shots


logger = logging.getLogger(__name__)


def _coerce_float(value: object) -> float:
    """Best-effort float coercion for timeline/cache values.

    We explicitly narrow types so the type-checker can validate the call to
    `float(...)`.

    Raises:
        TypeError: If the value cannot be coerced to a float.

    """
    if isinstance(value, (int, float, str, Decimal)):
        return float(value)
    raise TypeError(f"Unsupported float value type: {type(value)!r}")


class PlayerStatRow(TypedDict):
    """Structured representation of player statistics."""

    username: str
    shots_for: int
    shots_against: int
    goals_for: int
    goals_against: int
    impact_score: float
    impact_is_stored: bool
    minutes_played: float


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _compute_impact_score(*, gf: int, ga: int, sf: int, sa: int) -> float:
    """Compute a lightweight, aggregated impact score.

    This intentionally mirrors the frontend heuristic used in
    `PlayerStatsTable.computeImpactScore` so that team-season stats can be
    computed server-side without fetching match timelines.
    """
    acc_for = _safe_ratio(gf, sf)
    acc_against = _safe_ratio(ga, sa)
    efficiency_delta = acc_for - acc_against

    raw = gf * 8 - ga * 6 + (sf - sa) * 1.25 + efficiency_delta * 10
    return round(float(raw), 1)


def _ensure_latest_match_impacts(*, match_dataset: Iterable[Any]) -> None:
    """Ensure that finished matches have up-to-date persisted impact rows.

    The Team page aggregates `PlayerMatchImpact` across a season. If those rows
    are missing (or were computed with an older algorithm version), the totals
    diverge from the Match page.

    This function opportunistically recomputes missing/outdated impacts.
    It must be safe to call during request handling.
    """
    # MatchData is always expected in practice (TeamViewSet passes a QuerySet),
    # but keep this defensive to avoid breaking other call sites.
    try:
        if isinstance(match_dataset, QuerySet) and match_dataset.model is MatchData:
            match_qs = cast(QuerySet[MatchData], match_dataset)
        else:
            match_ids: list[str] = []
            for item in match_dataset:
                mid = getattr(item, "id_uuid", None)
                if mid is None:
                    continue
                match_ids.append(str(mid))

            if not match_ids:
                return
            match_qs = MatchData.objects.filter(id_uuid__in=match_ids)

        needs_recompute = (
            match_qs
            .filter(status="finished")
            # Recompute if there are no impacts at the latest version.
            .exclude(
                player_impacts__algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION
            )
            .distinct()
            .select_related("match_link")
        )

        for match_data in needs_recompute:
            try:
                rows = persist_match_impact_rows(
                    match_data=match_data,
                    algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
                )
                logger.info(
                    "Computed match impacts for %s (%s rows)",
                    match_data.id_uuid,
                    rows,
                )
            except Exception:
                # Never fail the team page because a single match can't be
                # recomputed. We'll fall back to the heuristic impact for any
                # players that are still missing persisted values.
                logger.exception(
                    "Failed to compute match impacts for %s", match_data.id_uuid
                )
    except Exception:
        logger.exception("Failed to ensure latest match impacts")


def _resolve_match_queryset(match_dataset: Iterable[Any]) -> QuerySet[MatchData] | None:
    """Best-effort normalization of match datasets to a MatchData queryset."""
    try:
        if isinstance(match_dataset, QuerySet) and match_dataset.model is MatchData:
            return cast(QuerySet[MatchData], match_dataset)

        match_ids: list[str] = []
        for item in match_dataset:
            mid = getattr(item, "id_uuid", None)
            if mid is None:
                continue
            match_ids.append(str(mid))

        if not match_ids:
            return None

        return MatchData.objects.filter(id_uuid__in=match_ids)
    except Exception:
        logger.exception("Failed to resolve match queryset")
        return None


def _sum_on_field_minutes(intervals: object, match_end_minutes: float) -> float:
    """Sum on-field minutes from RoleIntervals.

    On-field semantics: aanval + verdediging + unknown (reserve is off-field).
    """

    def _safe_interval_sum(items: Sequence[object]) -> float:
        total = 0.0
        for item in items:
            try:
                start_raw = getattr(item, "start")  # noqa: B009
                end_raw = getattr(item, "end")  # noqa: B009
                start = _coerce_float(start_raw)
                end = _coerce_float(end_raw)
            except Exception:
                # Defensive: historical data can contain malformed intervals.
                # Keep pages responsive and log for later inspection.
                logger.debug(
                    "Invalid role interval item while computing minutes; skipping",
                    exc_info=True,
                )
                continue
            start = max(0.0, min(start, match_end_minutes))
            end = max(0.0, min(end, match_end_minutes))
            if end > start:
                total += end - start
        return total

    return (
        _safe_interval_sum(getattr(intervals, "aanval", ()))
        + _safe_interval_sum(getattr(intervals, "verdediging", ()))
        + _safe_interval_sum(getattr(intervals, "unknown", ()))
    )


def _coerce_cached_minutes(cached: dict[object, object]) -> dict[str, float]:
    """Coerce cached minutes payload to a `dict[str, float]` safely."""
    out: dict[str, float] = {}
    for key, value in cached.items():
        try:
            pid = str(key)
            minutes = _coerce_float(value)
        except Exception:
            logger.debug(
                "Invalid cached minutes entry; skipping",
                exc_info=True,
            )
            continue
        out[pid] = minutes
    return out


def _collect_known_player_ids(
    *,
    groups: list[PlayerGroup],
    events: list[dict[str, Any]],
) -> set[str]:
    known_player_ids: set[str] = set()
    for group in groups:
        for player in group.players.all():
            pid = str(getattr(player, "id_uuid", "") or "").strip()
            if pid:
                known_player_ids.add(pid)

    # Substitution payloads may reference IDs that aren't present in groups due
    # to incomplete historical data. Include them defensively.
    for event in events:
        for key in ("player_in_id", "player_out_id", "player_id"):
            pid = str(event.get(key) or "").strip()
            if pid:
                known_player_ids.add(pid)

    return known_player_ids


def _minutes_played_by_player_id_for_match(match_data: MatchData) -> dict[str, float]:
    """Compute minutes played per player for a single match.

    Uses the same player-role timeline logic as match impact.
    Cached per match to keep team page requests fast.
    """
    cache_key = f"korfbal:match-minutes:v1:{match_data.id_uuid}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return _coerce_cached_minutes(cached)

    events = build_match_events(match_data)
    shots = build_match_shots(match_data)
    match_end_minutes = compute_match_end_minutes(events=events, shots=shots)

    groups = list(
        PlayerGroup.objects
        .select_related("starting_type", "team")
        .prefetch_related("players")
        .filter(match_data=match_data)
    )

    known_player_ids = _collect_known_player_ids(groups=groups, events=events)

    role_intervals_by_id = build_match_player_role_timeline(
        known_player_ids=sorted(known_player_ids),
        groups=groups,
        events=events,
        match_end_minutes=match_end_minutes,
    )

    minutes_by_player_id: dict[str, float] = {}
    for pid, intervals in role_intervals_by_id.items():
        minutes_by_player_id[pid] = round(
            _sum_on_field_minutes(intervals, match_end_minutes),
            2,
        )

    # Cache for 6 hours; enough to avoid repeat computations during browsing.
    cache.set(cache_key, minutes_by_player_id, timeout=60 * 60 * 6)
    return minutes_by_player_id


def _minutes_played_by_username(
    *, players: list[Any], match_dataset: Iterable[Any]
) -> dict[str, float]:
    match_qs = _resolve_match_queryset(match_dataset)
    if match_qs is None:
        return {}

    matches = list(match_qs.filter(status="finished"))
    if not matches:
        return {}

    minutes_by_player_id: dict[str, float] = {}
    for match_data in matches:
        per_match = _minutes_played_by_player_id_for_match(match_data)
        for pid, minutes in per_match.items():
            if minutes <= 0:
                continue
            minutes_by_player_id[pid] = minutes_by_player_id.get(pid, 0.0) + minutes

    minutes_by_username: dict[str, float] = {}
    for player in players:
        pid = str(getattr(player, "id_uuid", "") or "").strip()
        username = str(
            getattr(getattr(player, "user", None), "username", "") or ""
        ).strip()
        if not pid or not username:
            continue
        minutes_by_username[username] = round(minutes_by_player_id.get(pid, 0.0), 2)

    return minutes_by_username


async def build_player_stats(
    players: list[Any], match_dataset: Iterable[Any]
) -> list[PlayerStatRow]:
    """Compute the raw player statistics for a collection of matches.

    Returns:
        list[PlayerStatRow]: List of player stats.

    """
    if not players:
        return []

    def _fetch() -> tuple[list[dict[str, object]], dict[str, float], dict[str, float]]:
        _ensure_latest_match_impacts(match_dataset=match_dataset)

        minutes_by_username = _minutes_played_by_username(
            players=players,
            match_dataset=match_dataset,
        )

        shot_rows = list(
            Shot.objects
            .filter(
                match_data__in=match_dataset,
                player__in=players,
            )
            .values("player__user__username")
            .annotate(
                shots_for=Count("id_uuid", filter=Q(for_team=True)),
                shots_against=Count("id_uuid", filter=Q(for_team=False)),
                goals_for=Count("id_uuid", filter=Q(for_team=True, scored=True)),
                goals_against=Count("id_uuid", filter=Q(for_team=False, scored=True)),
            )
            .order_by("-goals_for", "player__user__username")
        )

        impact_rows = (
            PlayerMatchImpact.objects
            .filter(
                match_data__in=match_dataset,
                player__in=players,
                algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            )
            .values("player__user__username")
            .annotate(total=Sum("impact_score"))
        )

        impact_by_username: dict[str, float] = {}
        for row in impact_rows:
            username = str(row.get("player__user__username") or "").strip()
            total = row.get("total")
            if not username or total is None:
                continue
            impact_by_username[username] = round(float(total), 1)

        return shot_rows, impact_by_username, minutes_by_username

    rows, impact_by_username, minutes_by_username = await sync_to_async(_fetch)()

    player_rows: list[PlayerStatRow] = [  # type: ignore[invalid-assignment]
        {
            "username": (username := str(row.get("player__user__username") or "")),
            "shots_for": (sf := int(cast(int, row.get("shots_for") or 0))),
            "shots_against": (sa := int(cast(int, row.get("shots_against") or 0))),
            "goals_for": (gf := int(cast(int, row.get("goals_for") or 0))),
            "goals_against": (ga := int(cast(int, row.get("goals_against") or 0))),
            "impact_score": impact_by_username.get(
                username,
                _compute_impact_score(gf=gf, ga=ga, sf=sf, sa=sa),
            ),
            "impact_is_stored": username in impact_by_username,
            "minutes_played": float(minutes_by_username.get(username, 0.0)),
        }
        for row in rows
        if row.get("player__user__username")
    ]

    return sorted(player_rows, key=operator.itemgetter("goals_for"), reverse=True)


async def players_stats(players: list[Any], match_dataset: Iterable[Any]) -> str:
    """Return statistics of players in a match as websocket-friendly JSON.

    Returns:
        str: JSON string of player stats.

    """
    player_rows = await build_player_stats(players, match_dataset)
    return json.dumps(
        {
            "command": "stats",
            "data": {"type": "player_stats", "stats": {"player_stats": player_rows}},
        },
    )
