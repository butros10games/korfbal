"""Record each poule's competition period with the shared domain rules.

Live publication and historical import both call :func:`decide_pool_period`
(through :func:`resolve_pool_periods` or directly), so a poule's period never
depends on which import path observed it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from typing import Any

from django.db.models import Max, Min
from django.utils import timezone

from apps.competition.domain.classification import classify
from apps.competition.domain.competition_periods import (
    INDOOR,
    PhaseDecision,
    indoor_parts,
    outdoor_phase,
)
from apps.competition.models import Match, Pool, PoolEntry, SeasonBinding
from apps.competition.services.classification import map_pool
from apps.competition.services.seasons import target_season
from apps.schedule.queries.seasons import season_edition


OUTDOOR_SPORT = "KORFBALL-VE-WK"
INDOOR_SPORT = "KORFBALL-ZA-WK"
PERIOD_VERSION = "knkv-pool-period-v2"
MAX_PERIOD_BATCH = 5000


def period_backlog(limit: int = 200) -> list[int]:
    """Select never-attempted blank contexts in one indexed bounded batch.

    Raises:
        ValueError: The batch limit is invalid.

    """
    if not 0 < limit <= MAX_PERIOD_BATCH:
        raise ValueError("Period backlog limit must be between 1 and 5000")
    return list(
        Pool.objects
        .filter(phase="", phase_evidence={})
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )


def season_phase_mismatch(pool: Pool, phase: str | None) -> bool:
    """Self-routed playing seasons cannot silently turn into another period."""
    return bool(
        phase
        and pool.season.phase in {"autumn", "spring", "indoor", "full_season"}
        and pool.season.phase != phase
        and not SeasonBinding.objects
        .filter(scope=pool.season)
        .exclude(season=pool.season)
        .exists()
    )


def decide_pool_period(
    *, sport: str, class_name: str, edition: int | None, days: Iterable[date]
) -> PhaseDecision:
    """Decide one poule's period from its label and all of its fixture days.

    Returns:
        The phase decision; indoor parts need team evidence and are added later.

    """
    if sport in {INDOOR_SPORT, "KORFBALL-ZA-BK"}:
        return PhaseDecision(INDOOR, {"reason": "discipline"})
    if sport not in {OUTDOOR_SPORT, "KORFBALL-VE-BK"} or edition is None:
        return PhaseDecision(None, {"reason": "unsupported_context"})
    label = classify(class_name, sport, edition)[0].phase
    return outdoor_phase(
        days, edition, label if label in {"autumn", "spring"} else None
    )


def published(pool_ids: Iterable[int]) -> set[int]:
    """Return poules with a fixture already published under their period."""
    return set(
        Match.objects
        .filter(pool_id__in=list(pool_ids), local_match__isnull=False)
        .values_list("pool_id", flat=True)
        .distinct()
    )


def resolve_pool_periods(pool_ids: Iterable[int]) -> dict[str, int]:
    """Record periods of the given poules, bounded to them and their teams.

    A period that already routed a published fixture is never changed here:
    disagreeing new evidence is recorded for the reviewed repair command.
    Poules locked by a concurrent import are skipped and retried next pass.

    Returns:
        Counts of decided, conflicting and deferred poules.

    """
    ids = sorted(set(pool_ids))
    counts: dict[str, int] = defaultdict(int)
    if not ids:
        return counts
    pools = list(
        Pool.objects
        .select_for_update(of=("self",), no_key=True, skip_locked=True)
        .select_related("season")
        .filter(pk__in=ids)
        .order_by("pk")
    )
    counts["deferred"] = len(ids) - len(pools)
    days: dict[int, list[date]] = defaultdict(list)
    for pool_id, starts_at in Match.objects.filter(
        pool_id__in=[pool.pk for pool in pools]
    ).values_list("pool_id", "starts_at"):
        days[pool_id].append(timezone.localdate(starts_at))
    parts = _indoor_parts([pool for pool in pools if pool.sport == INDOOR_SPORT])
    frozen = published(pool.pk for pool in pools)
    for pool in pools:
        decision = decide_pool_period(
            sport=pool.sport,
            class_name=pool.class_name,
            edition=season_edition(pool.season),
            days=days[pool.pk],
        )
        if season_phase_mismatch(pool, decision.phase):
            decision = PhaseDecision(
                None,
                {
                    **decision.evidence,
                    "observed_phase": decision.phase,
                    "season_phase": pool.season.phase,
                    "reason": "season_phase_mismatch",
                },
            )
        part = parts.get(pool.pk)
        _apply(pool, decision, part, frozen=pool.pk in frozen, counts=counts)
    return counts


def _apply(
    pool: Pool,
    decision: PhaseDecision,
    part: PhaseDecision | None,
    *,
    frozen: bool,
    counts: dict[str, int],
) -> None:
    """Store a decision; published periods only gain review evidence."""
    phase = decision.phase or ""
    number = part.evidence.get("part") if part else None
    evidence: dict[str, Any] = {
        "version": PERIOD_VERSION,
        "decision": decision.evidence,
    }
    if part is not None:
        evidence["part"] = part.evidence
    target = target_season(pool.season, pool.sport, phase)
    route_conflict = (
        frozen
        and phase
        and (
            target is None
            or Match.objects
            .filter(pool=pool, local_match__isnull=False)
            .exclude(local_match__season=target)
            .exists()
        )
    )
    if route_conflict or (
        frozen
        and pool.phase
        and (phase, number)
        != (
            pool.phase,
            pool.competition_part,
        )
    ):
        evidence = {
            **pool.phase_evidence,
            "version": PERIOD_VERSION,
            "review": {
                "observed": phase,
                "reason": "season_repair_required"
                if route_conflict
                else "period_changed",
                **evidence,
            },
        }
        values: dict[str, Any] = {"phase_evidence": evidence}
        counts["conflicts"] += 1
    elif frozen and pool.phase:
        # Evidence agrees with the published period again: nothing to review.
        values = (
            {
                "phase_evidence": {
                    key: value
                    for key, value in pool.phase_evidence.items()
                    if key != "review"
                }
            }
            if "review" in pool.phase_evidence
            else {}
        )
    else:
        values = {
            "phase": phase,
            "competition_part": number if isinstance(number, int) else None,
            "phase_evidence": evidence,
        }
        counts["decided" if phase else "unresolved"] += 1
    changed = {
        key: value for key, value in values.items() if getattr(pool, key) != value
    }
    if changed:
        Pool.objects.filter(pk=pool.pk).update(**changed)
        for key, value in changed.items():
            setattr(pool, key, value)
        if "phase" in changed:
            # The class (and linked allocation baselines) follow the period's
            # native season, exactly as routing does.
            result = map_pool(pool)
            counts["allocations_realigned"] += result["allocations_realigned"]


def four_player(pool: Pool) -> bool:
    """Tell whether a poule's label explicitly names a four-player competition.

    KNKV plays only its four-player youth indoor competition in two separately
    graded parts; other indoor poules (including play-offs) are not parts.
    """
    return (
        classify(pool.class_name, pool.sport, season_edition(pool.season))[
            0
        ].playing_format
        == "four"
    )


def _indoor_parts(pools: list[Pool]) -> dict[int, PhaseDecision]:
    """Decide indoor parts from the sequences of teams entered in these poules."""
    pools = [pool for pool in pools if four_player(pool)]
    if not pools:
        return {}
    scopes = {pool.season_id for pool in pools}
    teams = set(
        PoolEntry.objects.filter(pool__in=pools).values_list("team_id", flat=True)
    )
    spans = {
        row["pool_id"]: (row["first"], row["last"])
        for row in Match.objects
        .filter(pool__season_id__in=scopes, pool__sport=INDOOR_SPORT)
        .filter(pool__entries__team_id__in=teams)
        .values("pool_id")
        .annotate(first=Min("starts_at"), last=Max("starts_at"))
    }
    spans = {
        pool.pk: spans[pool.pk]
        for pool in Pool.objects.filter(pk__in=spans).select_related("season")
        if four_player(pool)
    }
    sequences: dict[int, list[tuple[str, date, date]]] = defaultdict(list)
    for team_id, pool_id in PoolEntry.objects.filter(
        team_id__in=teams, pool_id__in=spans
    ).values_list("team_id", "pool_id"):
        first, last = spans[pool_id]
        sequences[team_id].append((
            str(pool_id),
            timezone.localdate(first),
            timezone.localdate(last),
        ))
    decisions = indoor_parts(sequences.values())
    return {
        pool.pk: decisions[str(pool.pk)] for pool in pools if str(pool.pk) in decisions
    }
