"""Plan and resolve independent four-team finals brackets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Max

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentFinalGroup,
    TournamentMatch,
    TournamentPool,
    TournamentStage,
    TournamentTeam,
)
from apps.tournament.services.match_operations import (
    TournamentMatchOperationError,
    replace_scheduled_match_teams,
)
from apps.tournament.services.qualifiers import evaluate_best_rank, evaluate_pool_rank
from apps.tournament.services.standings import calculate_pool_standings


class FinalGroupError(ValueError):
    """Raised when a final-group plan or qualifier transition is unsafe."""


MIN_POOL_TEAMS = 2
MAX_MATCH_DURATION_MINUTES = 240


@dataclass(frozen=True, slots=True)
class FinalMatchPlan:
    """Scheduling values for one match in a final group."""

    field_id: UUID
    starts_at: datetime
    duration_minutes: int

    @property
    def ends_at(self) -> datetime:
        """Return the exclusive end of the planned match."""
        return self.starts_at + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True, slots=True)
class FinalGroupPlan:
    """A named pair of semifinals followed by one final."""

    name: str
    format: str
    pool_ids: tuple[UUID, ...]
    semifinals: tuple[FinalMatchPlan, FinalMatchPlan]
    final: FinalMatchPlan


def _pool_count(format_name: str) -> int:
    counts: dict[str, int] = {
        TournamentFinalGroup.Format.THREE_POOL_WILDCARD.value: 3,
        TournamentFinalGroup.Format.TWO_POOL_CROSS.value: 2,
    }
    try:
        return counts[format_name]
    except KeyError as exc:
        raise FinalGroupError("Select a supported final-group format.") from exc


def _objects_for_plan(
    tournament: Tournament,
    plan: FinalGroupPlan,
) -> tuple[list[TournamentPool], dict[UUID, TournamentField]]:
    expected_pools = _pool_count(plan.format)
    if (
        len(plan.pool_ids) != expected_pools
        or len(set(plan.pool_ids)) != expected_pools
    ):
        raise FinalGroupError(
            f"Select {expected_pools} different pools for this final-group format."
        )
    pools_by_id = {
        pool.id_uuid: pool
        for pool in tournament.pools.filter(
            id_uuid__in=plan.pool_ids,
            stage__kind=TournamentStage.Kind.POOL,
        ).prefetch_related("entries")
    }
    if len(pools_by_id) != expected_pools:
        raise FinalGroupError("Select pool stages from this tournament.")
    pools = [pools_by_id[pool_id] for pool_id in plan.pool_ids]
    if any(pool.entries.count() < MIN_POOL_TEAMS for pool in pools):
        raise FinalGroupError("Every selected pool needs at least two teams.")
    if any(not pool.matches.exists() for pool in pools):
        raise FinalGroupError(
            "Schedule the selected pools before planning their finals."
        )
    if plan.format == TournamentFinalGroup.Format.THREE_POOL_WILDCARD:
        pool_sizes = {pool.entries.count() for pool in pools}
        if len(pool_sizes) != 1:
            raise FinalGroupError(
                "Wildcard pools must contain the same number of teams."
            )

    field_ids = {match_plan.field_id for match_plan in (*plan.semifinals, plan.final)}
    fields = {
        field.id_uuid: field
        for field in tournament.fields.filter(id_uuid__in=field_ids, active=True)
    }
    if len(fields) != len(field_ids):
        raise FinalGroupError("Select active fields from this tournament.")
    return pools, fields


def _validate_schedule(
    tournament: Tournament,
    plan: FinalGroupPlan,
    fields: dict[UUID, TournamentField],
) -> None:
    all_plans = (*plan.semifinals, plan.final)
    if any(
        item.duration_minutes < 1 or item.duration_minutes > MAX_MATCH_DURATION_MINUTES
        for item in all_plans
    ):
        raise FinalGroupError("Match duration must be between 1 and 240 minutes.")
    semifinal_end = max(item.ends_at for item in plan.semifinals)
    if plan.final.starts_at < semifinal_end:
        raise FinalGroupError("The final must start after both semifinals finish.")

    for index, left in enumerate(all_plans):
        for right in all_plans[index + 1 :]:
            if (
                left.field_id == right.field_id
                and left.starts_at < right.ends_at
                and right.starts_at < left.ends_at
            ):
                raise FinalGroupError("Two final-group matches overlap on one field.")
        existing = tournament.matches.filter(
            field=fields[left.field_id],
            starts_at__isnull=False,
        ).exclude(status=TournamentMatch.Status.CANCELLED)
        for other in existing:
            if other.starts_at is None:
                continue
            other_end = other.starts_at + timedelta(minutes=other.duration_minutes)
            if left.starts_at < other_end and other.starts_at < left.ends_at:
                raise FinalGroupError(
                    f"The planned time overlaps with match {other.match_number} "
                    f'on field "{fields[left.field_id].label}".'
                )


def _pool_source(pool: TournamentPool, rank: int) -> dict[str, Any]:
    return {"kind": "pool_rank", "pool_ids": [str(pool.id_uuid)], "rank": rank}


def _best_source(pools: list[TournamentPool], rank: int) -> dict[str, Any]:
    return {
        "kind": "best_rank",
        "pool_ids": [str(pool.id_uuid) for pool in pools],
        "rank": rank,
    }


def _semifinal_sources(
    format_name: str,
    pools: list[TournamentPool],
) -> tuple[tuple[dict[str, Any], dict[str, Any]], ...]:
    if format_name == TournamentFinalGroup.Format.THREE_POOL_WILDCARD:
        return (
            (_pool_source(pools[0], 1), _pool_source(pools[1], 1)),
            (_pool_source(pools[2], 1), _best_source(pools, 2)),
        )
    return (
        (_pool_source(pools[0], 2), _pool_source(pools[1], 1)),
        (_pool_source(pools[0], 1), _pool_source(pools[1], 2)),
    )


def _stage_name(group_name: str, suffix: str) -> str:
    return f"{group_name} · {suffix}"


@transaction.atomic
def create_final_group(
    tournament: Tournament,
    *,
    plan: FinalGroupPlan,
) -> TournamentFinalGroup:
    """Persist a reviewable final group and resolve entrants when possible.

    Raises:
        FinalGroupError: If sources, fields, or times are inconsistent.

    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    name = " ".join(plan.name.split())
    if not name:
        raise FinalGroupError("Give the final group a name.")
    if tournament.final_groups.filter(name__iexact=name).exists():
        raise FinalGroupError("A final group with this name already exists.")
    if tournament.stages.filter(
        kind__in=[TournamentStage.Kind.KNOCKOUT, TournamentStage.Kind.FINAL],
        final_group__isnull=True,
    ).exists():
        raise FinalGroupError(
            "Remove the existing simple final round before adding final groups."
        )
    stage_names = (_stage_name(name, "Halve finales"), _stage_name(name, "Finale"))
    if tournament.stages.filter(name__in=stage_names).exists():
        raise FinalGroupError("A stage with this final-group name already exists.")

    pools, fields = _objects_for_plan(tournament, plan)
    _validate_schedule(tournament, plan, fields)
    next_group_order = (
        tournament.final_groups.aggregate(value=Max("sort_order"))["value"] or 0
    ) + 1
    group = TournamentFinalGroup.objects.create(
        tournament=tournament,
        name=name,
        format=plan.format,
        sort_order=next_group_order,
    )
    next_stage_order = (
        tournament.stages.aggregate(value=Max("sort_order"))["value"] or 0
    ) + 1
    semifinal_stage = TournamentStage.objects.create(
        tournament=tournament,
        final_group=group,
        name=stage_names[0],
        kind=TournamentStage.Kind.KNOCKOUT,
        sort_order=next_stage_order,
    )
    final_stage = TournamentStage.objects.create(
        tournament=tournament,
        final_group=group,
        name=stage_names[1],
        kind=TournamentStage.Kind.FINAL,
        sort_order=next_stage_order + 1,
    )
    next_match_number = (
        tournament.matches.aggregate(value=Max("match_number"))["value"] or 0
    ) + 1
    semifinals: list[TournamentMatch] = []
    for index, (match_plan, sources) in enumerate(
        zip(plan.semifinals, _semifinal_sources(plan.format, pools), strict=True)
    ):
        semifinal = TournamentMatch.objects.create(
            tournament=tournament,
            stage=semifinal_stage,
            field=fields[match_plan.field_id],
            round_number=1,
            match_number=next_match_number + index,
            starts_at=match_plan.starts_at,
            duration_minutes=match_plan.duration_minutes,
            home_qualifier=sources[0],
            away_qualifier=sources[1],
        )
        semifinals.append(semifinal)
    final = TournamentMatch.objects.create(
        tournament=tournament,
        stage=final_stage,
        field=fields[plan.final.field_id],
        round_number=2,
        match_number=next_match_number + 2,
        starts_at=plan.final.starts_at,
        duration_minutes=plan.final.duration_minutes,
    )
    for index, semifinal in enumerate(semifinals):
        semifinal.next_match = final
        semifinal.winner_to_side = (
            TournamentMatch.DestinationSide.HOME
            if index == 0
            else TournamentMatch.DestinationSide.AWAY
        )
        semifinal.save(update_fields=["next_match", "winner_to_side", "updated_at"])
    resolve_tournament_qualifiers(tournament, group=group)
    return group


def _source_pools(
    tournament: Tournament,
    source: dict[str, Any],
) -> list[TournamentPool]:
    pool_ids = source.get("pool_ids")
    if not isinstance(pool_ids, list) or not pool_ids:
        raise FinalGroupError("A final qualifier has no source pools.")
    lookup = {
        str(pool.id_uuid): pool
        for pool in tournament.pools.filter(id_uuid__in=pool_ids).prefetch_related(
            "entries__team",
            "entries__adjustments",
            "matches",
        )
    }
    try:
        return [lookup[str(pool_id)] for pool_id in pool_ids]
    except KeyError as exc:
        raise FinalGroupError("A final qualifier references a missing pool.") from exc


def _resolved_team(
    tournament: Tournament,
    source: dict[str, Any],
) -> TournamentTeam | None:
    if not source:
        return None
    pools = _source_pools(tournament, source)
    rank = source.get("rank")
    if not isinstance(rank, int) or rank < 1:
        raise FinalGroupError("A final qualifier has an invalid pool position.")
    for pool in pools:
        standings = calculate_pool_standings(pool)
        if rank > len(standings):
            raise FinalGroupError(f'Pool "{pool.name}" has no position {rank}.')
    if source.get("kind") == "pool_rank" and len(pools) == 1:
        decision = evaluate_pool_rank(pools[0], rank)
    elif source.get("kind") == "best_rank" and len(pools) > 1:
        decision = evaluate_best_rank(tournament, pools, rank)
    else:
        raise FinalGroupError("A final qualifier has an invalid source type.")
    if decision.decided_team_id is None:
        return None
    return tournament.teams.get(pk=decision.decided_team_id)


def _match_is_locked(match: TournamentMatch) -> bool:
    return (
        match.status != TournamentMatch.Status.SCHEDULED
        or match.home_score is not None
        or match.away_score is not None
    )


@transaction.atomic
def resolve_tournament_qualifiers(
    tournament: Tournament,
    *,
    group: TournamentFinalGroup | None = None,
) -> None:
    """Fill or clear planned bracket entrants from authoritative standings.

    Raises:
        FinalGroupError: If a source is invalid or a locked entrant would change.

    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    matches = (
        TournamentMatch.objects
        .select_for_update(of=("self",))
        .filter(
            tournament=tournament,
        )
        .exclude(stage__kind=TournamentStage.Kind.POOL)
    )
    if group is not None:
        matches = matches.filter(stage__final_group=group)
    for match in matches.select_related("home_team", "away_team"):
        sources = {
            "home_team": match.home_qualifier,
            "away_team": match.away_qualifier,
        }
        replacements = {
            field: _resolved_team(tournament, source)
            for field, source in sources.items()
            if source
        }
        changed_fields = [
            field
            for field, replacement in replacements.items()
            if getattr(match, f"{field}_id") != getattr(replacement, "pk", None)
        ]
        if not changed_fields:
            continue
        if _match_is_locked(match):
            raise FinalGroupError(
                f"Match {match.match_number} already started; "
                "its qualifiers cannot change."
            )
        try:
            replace_scheduled_match_teams(match, replacements)
        except TournamentMatchOperationError as exc:
            raise FinalGroupError(str(exc)) from exc


def qualifier_label(tournament: Tournament, source: dict[str, Any]) -> str | None:
    """Return a stable Dutch placeholder label for one qualifier source."""
    if not source:
        return None
    pools = _source_pools(tournament, source)
    rank = source.get("rank")
    if source.get("kind") == "pool_rank" and len(pools) == 1:
        return f"{pools[0].name} #{rank}"
    if source.get("kind") == "best_rank":
        names = ", ".join(pool.name for pool in pools)
        return f"Beste #{rank} van {names}"
    return None


@transaction.atomic
def delete_final_group(
    tournament: Tournament,
    group: TournamentFinalGroup,
) -> None:
    """Delete a final group while none of its matches contains live data.

    Raises:
        FinalGroupError: If at least one bracket match has started or has a score.

    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    group = tournament.final_groups.select_for_update().get(pk=group.pk)
    matches = tournament.matches.filter(stage__final_group=group)
    if (
        matches.exclude(status=TournamentMatch.Status.SCHEDULED).exists()
        or matches.filter(home_score__isnull=False).exists()
        or matches.filter(away_score__isnull=False).exists()
    ):
        raise FinalGroupError("Only an unstarted final group can be deleted.")
    group.delete()
