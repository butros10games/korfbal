"""Pure pool allocation and conflict-safe field scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from typing import Any

from django.db import transaction
from django.db.models import Q

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentPool,
    TournamentPoolEntry,
    TournamentStage,
    TournamentTeam,
)


MIN_TEAMS = 2
MAX_POOLS = 26


class GenerationError(ValueError):
    """Raised when a tournament cannot produce a valid plan."""


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    """Validated inputs shared by preview and apply operations."""

    pool_count: int
    strategy: str = "snake"
    random_seed: int = 1
    legs: int = 1
    starts_at: datetime | None = None
    duration_minutes: int | None = None
    changeover_minutes: int | None = None
    minimum_rest_minutes: int | None = None


def round_robin_rounds(
    team_ids: list[str],
) -> list[list[tuple[str, str]]]:
    """Return circle-method pairings where a team appears once per round."""
    participants: list[str | None] = list(team_ids)
    if len(participants) % 2:
        participants.append(None)
    if len(participants) < MIN_TEAMS:
        return []

    fixed = participants[0]
    rotating = participants[1:]
    rounds: list[list[tuple[str, str]]] = []
    for round_index in range(len(participants) - 1):
        current = [fixed, *rotating]
        pairings: list[tuple[str, str]] = []
        for index in range(len(current) // 2):
            left = current[index]
            right = current[-1 - index]
            if left is None or right is None:
                continue
            # Alternate the fixed participant's side to avoid a permanent home bias.
            if index == 0 and round_index % 2:
                left, right = right, left
            pairings.append((left, right))
        rounds.append(pairings)
        if rotating:
            rotating = [rotating[-1], *rotating[:-1]]
    return rounds


def allocate_pools(
    teams: list[TournamentTeam],
    *,
    pool_count: int,
    strategy: str,
    random_seed: int,
) -> list[list[TournamentTeam]]:
    """Allocate teams predictably using seed, snake, or random ordering.

    Raises:
        GenerationError: If the team or pool configuration is invalid.

    """
    if len(teams) < MIN_TEAMS:
        raise GenerationError("Add at least two active teams before generating pools.")
    maximum_pool_count = min(MAX_POOLS, len(teams) // MIN_TEAMS)
    if pool_count < 1 or pool_count > maximum_pool_count:
        raise GenerationError("Pool count must leave at least two teams in every pool.")

    ordered = sorted(teams, key=lambda team: (team.seed, team.sort_order, team.name))
    if strategy == "random":
        ordered.sort(
            key=lambda team: hashlib.sha256(
                f"{random_seed}:{team.id_uuid}".encode()
            ).digest()
        )
    elif strategy not in {"seeded", "snake"}:
        raise GenerationError("Unknown pool allocation strategy.")

    pools: list[list[TournamentTeam]] = [[] for _ in range(pool_count)]
    for index, team in enumerate(ordered):
        if strategy == "snake" and (index // pool_count) % 2:
            pool_index = pool_count - 1 - (index % pool_count)
        else:
            pool_index = index % pool_count
        pools[pool_index].append(team)
    return pools


def _pool_name(index: int) -> str:
    return f"Poule {chr(ord('A') + index)}"


def _ordered_pairings(
    pools: list[list[TournamentTeam]],
    *,
    legs: int,
) -> list[tuple[int, int, TournamentTeam, TournamentTeam]]:
    by_pool = [
        round_robin_rounds([str(team.id_uuid) for team in pool]) for pool in pools
    ]
    lookup = {str(team.id_uuid): team for pool in pools for team in pool}
    scheduled: list[tuple[int, int, TournamentTeam, TournamentTeam]] = []
    max_rounds = max((len(rounds) for rounds in by_pool), default=0)
    sequence = 0
    for leg in range(legs):
        for round_index in range(max_rounds):
            for pool_index, rounds in enumerate(by_pool):
                if round_index >= len(rounds):
                    continue
                for home_id, away_id in rounds[round_index]:
                    home = lookup[home_id]
                    away = lookup[away_id]
                    if leg % 2:
                        home, away = away, home
                    sequence += 1
                    scheduled.append((pool_index, sequence, home, away))
    return scheduled


def _resolved_pool_fields(
    fields: list[TournamentField],
    pool_fields: list[TournamentField | None] | None,
    *,
    pool_count: int,
) -> list[TournamentField | None]:
    if pool_fields is None:
        return [None] * pool_count
    if len(pool_fields) != pool_count:
        raise GenerationError("Every pool must have a corresponding field assignment.")

    active_fields = {field.pk: field for field in fields}
    resolved: list[TournamentField | None] = []
    for assigned_field in pool_fields:
        if assigned_field is None:
            resolved.append(None)
            continue
        field = active_fields.get(assigned_field.pk)
        if field is None:
            raise GenerationError(
                f'Assigned field "{assigned_field.label}" must be active.'
            )
        resolved.append(field)
    return resolved


def _scheduled_matches(
    tournament: Tournament,
    *,
    pools: list[list[TournamentTeam]],
    options: GenerationOptions,
    pool_fields: list[TournamentField | None] | None = None,
) -> list[dict[str, Any]]:
    """Assign every pool pairing to an available field and start time.

    Raises:
        GenerationError: If fields or timing options are invalid.

    """
    if options.legs not in {1, 2}:
        raise GenerationError("Legs must be 1 or 2.")
    teams = [team for pool in pools for team in pool]
    fields = list(tournament.fields.filter(active=True))
    if not fields:
        raise GenerationError("Add at least one active field before scheduling.")
    resolved_pool_fields = _resolved_pool_fields(
        fields,
        pool_fields,
        pool_count=len(pools),
    )

    start = options.starts_at or tournament.starts_at
    duration = options.duration_minutes or tournament.match_duration_minutes
    changeover = (
        tournament.changeover_minutes
        if options.changeover_minutes is None
        else options.changeover_minutes
    )
    minimum_rest = (
        tournament.minimum_rest_minutes
        if options.minimum_rest_minutes is None
        else options.minimum_rest_minutes
    )
    if duration < 1 or changeover < 0 or minimum_rest < 0:
        raise GenerationError("Duration and rest settings are invalid.")

    field_available = {str(field.id_uuid): start for field in fields}
    team_available = {str(team.id_uuid): start for team in teams}
    matches: list[dict[str, Any]] = []
    slot_starts: list[datetime] = []

    for pool_index, match_number, home, away in _ordered_pairings(
        pools, legs=options.legs
    ):
        home_id = str(home.id_uuid)
        away_id = str(away.id_uuid)
        candidates = []
        assigned_field = resolved_pool_fields[pool_index]
        eligible_fields = [assigned_field] if assigned_field else fields
        for field in eligible_fields:
            field_id = str(field.id_uuid)
            candidate_start = max(
                field_available[field_id],
                team_available[home_id],
                team_available[away_id],
            )
            candidates.append((candidate_start, field.sort_order, field.label, field))
        match_start, _, _, field = min(candidates)
        match_end = match_start + timedelta(minutes=duration)
        field_available[str(field.id_uuid)] = match_end + timedelta(minutes=changeover)
        next_team_time = match_end + timedelta(minutes=minimum_rest)
        team_available[home_id] = next_team_time
        team_available[away_id] = next_team_time
        if match_start not in slot_starts:
            slot_starts.append(match_start)
            slot_starts.sort()
        matches.append({
            "pool_index": pool_index,
            "match_number": match_number,
            "round_number": slot_starts.index(match_start) + 1,
            "home_team_id": home_id,
            "home_team_name": home.name,
            "away_team_id": away_id,
            "away_team_name": away.name,
            "field_id": str(field.id_uuid),
            "field_label": field.label,
            "starts_at": match_start.isoformat(),
            "duration_minutes": duration,
        })

    return matches


def build_pool_plan(
    tournament: Tournament,
    *,
    pool_count: int,
    strategy: str,
    random_seed: int = 1,
) -> list[dict[str, Any]]:
    """Build editable pool assignments without creating matches."""
    pools = allocate_pools(
        list(tournament.teams.filter(withdrawn=False)),
        pool_count=pool_count,
        strategy=strategy,
        random_seed=random_seed,
    )
    return [
        {
            "name": _pool_name(index),
            "teams": [
                {"id_uuid": str(team.id_uuid), "name": team.name} for team in pool
            ],
        }
        for index, pool in enumerate(pools)
    ]


def build_existing_pool_match_plan(
    tournament: Tournament,
    *,
    options: GenerationOptions,
) -> list[dict[str, Any]]:
    """Build a schedule from the pool assignments currently under review.

    Raises:
        GenerationError: If no playable pools exist.

    """
    pools = list(
        tournament.pools
        .filter(stage__kind=TournamentStage.Kind.POOL)
        .select_related("assigned_field")
        .prefetch_related("entries__team")
        .order_by("sort_order", "name")
    )
    team_groups = [[entry.team for entry in pool.entries.all()] for pool in pools]
    if not team_groups or any(len(teams) < MIN_TEAMS for teams in team_groups):
        raise GenerationError("Every pool needs at least two teams before scheduling.")
    return _scheduled_matches(
        tournament,
        pools=team_groups,
        options=options,
        pool_fields=[pool.assigned_field for pool in pools],
    )


def build_generation_plan(
    tournament: Tournament,
    *,
    options: GenerationOptions,
) -> dict[str, Any]:
    """Build a serializable combined pool and field plan for compatibility."""
    pool_plan = build_pool_plan(
        tournament,
        pool_count=options.pool_count,
        strategy=options.strategy,
        random_seed=options.random_seed,
    )
    teams = {str(team.id_uuid): team for team in tournament.teams.all()}
    pool_teams = [
        [teams[team["id_uuid"]] for team in pool["teams"]] for pool in pool_plan
    ]
    matches = _scheduled_matches(tournament, pools=pool_teams, options=options)
    return {
        "pool_count": options.pool_count,
        "strategy": options.strategy,
        "random_seed": options.random_seed,
        "legs": options.legs,
        "pools": pool_plan,
        "matches": matches,
        "warnings": [],
    }


def _create_pools(
    tournament: Tournament,
    *,
    pool_plan: list[dict[str, Any]],
) -> tuple[TournamentStage, list[TournamentPool]]:
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Poules",
        kind=TournamentStage.Kind.POOL,
        sort_order=1,
    )
    teams = {str(team.id_uuid): team for team in tournament.teams.all()}
    pools: list[TournamentPool] = []
    for pool_index, pool_data in enumerate(pool_plan):
        pool = TournamentPool.objects.create(
            tournament=tournament,
            stage=stage,
            name=pool_data["name"],
            sort_order=pool_index,
        )
        pools.append(pool)
        TournamentPoolEntry.objects.bulk_create([
            TournamentPoolEntry(
                pool=pool,
                team=teams[team_data["id_uuid"]],
                seed_order=entry_index + 1,
            )
            for entry_index, team_data in enumerate(pool_data["teams"])
        ])
    return stage, pools


@transaction.atomic
def apply_pool_plan(
    tournament: Tournament,
    *,
    pool_plan: list[dict[str, Any]],
) -> None:
    """Replace draft pools and leave them available for organizer review."""
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    ensure_schedule_replaceable(tournament)
    tournament.matches.all().delete()
    tournament.stages.all().delete()
    _create_pools(tournament, pool_plan=pool_plan)


@transaction.atomic
def apply_existing_pool_match_plan(
    tournament: Tournament,
    *,
    matches: list[dict[str, Any]],
) -> None:
    """Replace draft matches while preserving reviewed pool assignments.

    Raises:
        GenerationError: If there are no reviewed pools to schedule.

    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    ensure_schedule_replaceable(tournament)
    pools = list(
        tournament.pools.filter(stage__kind=TournamentStage.Kind.POOL).order_by(
            "sort_order", "name"
        )
    )
    if not pools:
        raise GenerationError("Create or generate pools before scheduling matches.")
    tournament.matches.all().delete()
    tournament.stages.exclude(kind=TournamentStage.Kind.POOL).delete()
    fields = {str(field.id_uuid): field for field in tournament.fields.all()}
    teams = {str(team.id_uuid): team for team in tournament.teams.all()}
    TournamentMatch.objects.bulk_create([
        TournamentMatch(
            tournament=tournament,
            stage=pools[match_data["pool_index"]].stage,
            pool=pools[match_data["pool_index"]],
            home_team=teams[match_data["home_team_id"]],
            away_team=teams[match_data["away_team_id"]],
            field=fields[match_data["field_id"]],
            round_number=match_data["round_number"],
            match_number=index,
            starts_at=datetime.fromisoformat(match_data["starts_at"]),
            duration_minutes=match_data["duration_minutes"],
        )
        for index, match_data in enumerate(matches, start=1)
    ])


@transaction.atomic
def apply_generation_plan(
    tournament: Tournament,
    *,
    plan: dict[str, Any],
) -> None:
    """Replace an unscored generated schedule with a reviewed plan."""
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    ensure_schedule_replaceable(tournament)

    tournament.matches.all().delete()
    tournament.stages.all().delete()
    stage, pools = _create_pools(tournament, pool_plan=plan["pools"])
    fields = {str(field.id_uuid): field for field in tournament.fields.all()}
    teams = {str(team.id_uuid): team for team in tournament.teams.all()}

    TournamentMatch.objects.bulk_create([
        TournamentMatch(
            tournament=tournament,
            stage=stage,
            pool=pools[match_data["pool_index"]],
            home_team=teams[match_data["home_team_id"]],
            away_team=teams[match_data["away_team_id"]],
            field=fields[match_data["field_id"]],
            round_number=match_data["round_number"],
            match_number=match_data["match_number"],
            starts_at=datetime.fromisoformat(match_data["starts_at"]),
            duration_minutes=match_data["duration_minutes"],
        )
        for match_data in plan["matches"]
    ])


def ensure_schedule_replaceable(tournament: Tournament) -> None:
    """Reject replacement once any match has started or contains a score.

    Raises:
        GenerationError: If replacing the schedule would destroy live data.

    """
    if tournament.matches.exclude(status=TournamentMatch.Status.SCHEDULED).exists():
        raise GenerationError(
            "A live or finalized schedule cannot be regenerated. "
            "Reopen or reset results first."
        )
    if tournament.matches.filter(
        Q(home_score__isnull=False) | Q(away_score__isnull=False)
    ).exists():
        raise GenerationError("Clear saved scores before regenerating the schedule.")
