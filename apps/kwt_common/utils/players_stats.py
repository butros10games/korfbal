"""Module contains `players_stats` function that returns player stats in a match.

Team/season pages should report impact scores consistent with the Match page.

We achieve this by:
- persisting per-player per-match impact rows using the match-page algorithm
- aggregating those persisted rows for team/season totals

When persisted rows are missing or outdated, we opportunistically recompute them.
"""

from __future__ import annotations

from collections.abc import Iterable
import json
import logging
import operator
from typing import Any, TypedDict, cast

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.core.cache.backends.dummy import DummyCache
from django.db.models import Count, Q, QuerySet, Sum

from apps.game_tracker.models import (
    MatchData,
    PlayerMatchImpact,
    PlayerMatchMinutes,
    Shot,
)
from apps.game_tracker.models.player_match_minutes import LATEST_MATCH_MINUTES_VERSION
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    persist_match_impact_rows,
)


logger = logging.getLogger(__name__)


class PlayerStatRow(TypedDict):
    """Structured representation of player statistics."""

    username: str
    shots_for: int
    shots_against: int
    goals_for: int
    goals_against: int
    impact_score: float
    impact_is_stored: bool
    minutes_played: float | None


def _impact_autorecompute_enabled() -> bool:
    # Default-on so Team/season pages match the canonical Match-page algorithm.
    # Can be disabled in settings for emergency performance mitigation.
    return bool(getattr(settings, "KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE", True))


def _impact_autorecompute_limit() -> int:
    # Hard cap safety: recomputing impacts is expensive.
    # Default higher than the historical value so season pages converge without
    # requiring manual backfills, while still keeping a hard ceiling.
    configured = getattr(settings, "KORFBAL_IMPACT_AUTO_RECOMPUTE_LIMIT", None)
    if configured is None:
        return 50
    return max(0, min(int(configured), 200))


def _persisted_minutes_by_username(
    *,
    players: list[Any],
    match_qs: QuerySet[MatchData],
) -> dict[str, float]:
    rows = (
        PlayerMatchMinutes.objects
        .filter(
            match_data__in=match_qs,
            player__in=players,
            algorithm_version=LATEST_MATCH_MINUTES_VERSION,
        )
        .values("player__user__username")
        .annotate(total=Sum("minutes_played"))
    )

    out: dict[str, float] = {}
    for row in rows:
        username = str(row.get("player__user__username") or "").strip()
        total = row.get("total")
        if not username or total is None:
            continue
        out[username] = round(float(total), 2)
    return out


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


def _matches_needing_impact_recompute(
    *, match_qs: QuerySet[MatchData]
) -> QuerySet[MatchData]:
    """Return finished matches missing latest-version persisted impacts."""
    return (
        match_qs
        .filter(status="finished")
        .exclude(
            player_impacts__algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION
        )
        .distinct()
        .select_related("match_link")
    )


def _acquire_impact_dataset_lock() -> bool:
    """Acquire a short-lived lock to avoid recompute stampedes."""
    # In tests we run many isolated scenarios in a single process. A global
    # dataset lock can unintentionally block later tests (and hide regressions)
    # because the cache is shared across test functions.
    if bool(getattr(settings, "TESTING", False)):
        return True

    # Some environments (notably certain test setups) use DummyCache, where
    # `add()` always returns False. Locks are best-effort; if we cannot lock,
    # continue rather than permanently disabling recompute.
    if isinstance(cache, DummyCache):
        return True
    dataset_lock_key = "korfbal:impact-autorecompute-dataset-lock"
    return bool(cache.add(dataset_lock_key, "1", timeout=60))


def _recompute_impacts_for_matches(*, matches: QuerySet[MatchData], limit: int) -> None:
    """Best-effort recompute impacts for up to `limit` matches."""
    if limit <= 0:
        return

    recomputed = 0
    for match_data in matches.iterator():
        if recomputed >= limit:
            break

        lock_key = (
            f"korfbal:impact-autorecompute-lock:{match_data.id_uuid}:"
            f"{LATEST_MATCH_IMPACT_ALGORITHM_VERSION}"
        )
        if not isinstance(cache, DummyCache) and not cache.add(
            lock_key, "1", timeout=60 * 10
        ):
            continue

        try:
            rows = persist_match_impact_rows(
                match_data=match_data,
                algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            )
        except Exception:
            # Never fail the team page because a single match can't be
            # recomputed. We'll fall back to the heuristic impact for any
            # players that are still missing persisted values.
            logger.exception(
                "Failed to compute match impacts for %s",
                match_data.id_uuid,
            )
            continue

        logger.info(
            "Computed match impacts for %s (%s rows)",
            match_data.id_uuid,
            rows,
        )
        recomputed += 1


def _ensure_latest_match_impacts(*, match_dataset: Iterable[Any]) -> None:
    """Ensure that finished matches have up-to-date persisted impact rows.

    The Team page aggregates `PlayerMatchImpact` across a season. If those rows
    are missing (or were computed with an older algorithm version), the totals
    diverge from the Match page.

    This function opportunistically recomputes missing/outdated impacts.
    It must be safe to call during request handling.
    """
    # IMPORTANT PERFORMANCE NOTE:
    # Recomputing missing/outdated impact rows during request handling can be
    # expensive. We keep cache locks to avoid stampedes and a configurable hard
    # cap on the number of matches recomputed per request.
    if not _impact_autorecompute_enabled():
        return

    limit = _impact_autorecompute_limit()
    if limit <= 0:
        return

    match_qs = _resolve_match_queryset(match_dataset)
    if match_qs is None:
        return

    needs_recompute = _matches_needing_impact_recompute(match_qs=match_qs)
    if not _acquire_impact_dataset_lock():
        return

    _recompute_impacts_for_matches(matches=needs_recompute, limit=limit)


def _dataset_has_complete_latest_impacts(*, match_dataset: Iterable[Any]) -> bool:
    """Return True when all finished matches have latest-version impact rows.

    This is used to avoid reporting partial persisted totals as if they were
    canonical.
    """
    match_qs = _resolve_match_queryset(match_dataset)
    if match_qs is None:
        return False

    finished_qs = match_qs.filter(status="finished")
    finished_count = finished_qs.count()
    if finished_count <= 0:
        return True

    impacted_match_count = (
        PlayerMatchImpact.objects
        .filter(
            match_data__in=finished_qs,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
        .values("match_data_id")
        .distinct()
        .count()
    )
    return impacted_match_count == finished_count


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


def _minutes_played_by_username(
    *, players: list[Any], match_dataset: Iterable[Any]
) -> dict[str, float]:
    match_qs = _resolve_match_queryset(match_dataset)
    if match_qs is None:
        return {}

    # Minutes-played should be computed only by the background task that
    # persists `PlayerMatchMinutes`. Request handlers should never recompute.
    return _persisted_minutes_by_username(players=players, match_qs=match_qs)


async def build_player_stats(
    players: list[Any], match_dataset: Iterable[Any]
) -> list[PlayerStatRow]:
    """Compute the raw player statistics for a collection of matches.

    Returns:
        list[PlayerStatRow]: List of player stats.

    """
    if not players:
        return []

    def _fetch() -> tuple[
        list[dict[str, object]],
        dict[str, float],
        dict[str, float],
        bool,
    ]:
        _ensure_latest_match_impacts(match_dataset=match_dataset)

        dataset_has_full_impacts = _dataset_has_complete_latest_impacts(
            match_dataset=match_dataset,
        )

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

        return (
            shot_rows,
            impact_by_username,
            minutes_by_username,
            dataset_has_full_impacts,
        )

    (
        rows,
        impact_by_username,
        minutes_by_username,
        dataset_has_full_impacts,
    ) = await sync_to_async(_fetch)()

    player_rows: list[PlayerStatRow] = [  # type: ignore[invalid-assignment]
        {
            "username": (username := str(row.get("player__user__username") or "")),
            "shots_for": (sf := int(cast(int, row.get("shots_for") or 0))),
            "shots_against": (sa := int(cast(int, row.get("shots_against") or 0))),
            "goals_for": (gf := int(cast(int, row.get("goals_for") or 0))),
            "goals_against": (ga := int(cast(int, row.get("goals_against") or 0))),
            "impact_score": (
                round(float(impact_by_username.get(username, 0.0)), 1)
                if dataset_has_full_impacts
                else _compute_impact_score(gf=gf, ga=ga, sf=sf, sa=sa)
            ),
            "impact_is_stored": bool(dataset_has_full_impacts),
            # Minutes-played are persisted asynchronously (Celery) into
            # PlayerMatchMinutes. When minutes are unavailable or missing for a
            # specific player, return null to avoid implying "0 minutes".
            "minutes_played": (
                float(minutes_by_username[username])
                if minutes_by_username and username in minutes_by_username
                else None
            ),
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
