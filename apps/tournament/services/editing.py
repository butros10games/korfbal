"""Transactional pool and match editing for tournament organizers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import Max, Q

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentPool,
    TournamentPoolEntry,
    TournamentStage,
    TournamentTeam,
)


class TournamentEditingError(ValueError):
    """Raised when a requested structural edit would be inconsistent."""


@dataclass(frozen=True, slots=True)
class MatchDraft:
    """Editable planning values for one tournament match."""

    pool_id: UUID
    home_team_id: UUID
    away_team_id: UUID
    field_id: UUID
    starts_at: datetime
    duration_minutes: int
    round_number: int


@dataclass(frozen=True, slots=True)
class _ResolvedMatchDraft:
    starts_at: datetime
    duration_minutes: int
    field: TournamentField
    home: TournamentTeam
    away: TournamentTeam


def _pool_stage(tournament: Tournament) -> TournamentStage:
    stage = tournament.stages.filter(kind=TournamentStage.Kind.POOL).first()
    if stage:
        return stage
    return TournamentStage.objects.create(
        tournament=tournament,
        name="Poules",
        kind=TournamentStage.Kind.POOL,
        sort_order=1,
    )


def _pool_teams(
    tournament: Tournament,
    team_ids: list[UUID],
    *,
    exclude_pool: TournamentPool | None = None,
) -> list[TournamentTeam]:
    if len(set(team_ids)) != len(team_ids):
        raise TournamentEditingError("Select each team only once.")
    teams = {
        team.id_uuid: team
        for team in tournament.teams.filter(id_uuid__in=team_ids, withdrawn=False)
    }
    if len(teams) != len(team_ids):
        raise TournamentEditingError("Select active teams from this tournament.")
    assigned = TournamentPoolEntry.objects.filter(
        pool__tournament=tournament,
        team_id__in=team_ids,
    )
    if exclude_pool:
        assigned = assigned.exclude(pool=exclude_pool)
    if assigned.exists():
        raise TournamentEditingError("A team can belong to only one pool.")
    return [teams[team_id] for team_id in team_ids]


def _ensure_pools_editable(tournament: Tournament) -> None:
    if tournament.matches.exists():
        raise TournamentEditingError(
            "Delete or regenerate the matches before changing pool assignments."
        )


@transaction.atomic
def create_pool(
    tournament: Tournament,
    *,
    name: str,
    team_ids: list[UUID],
) -> TournamentPool:
    """Create one manually composed pool.

    Raises:
        TournamentEditingError: If teams are unavailable or pools are locked.

    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    _ensure_pools_editable(tournament)
    if tournament.pools.filter(name__iexact=name).exists():
        raise TournamentEditingError("A pool with this name already exists.")
    teams = _pool_teams(tournament, team_ids)
    next_order = (tournament.pools.aggregate(value=Max("sort_order"))["value"] or 0) + 1
    pool = TournamentPool.objects.create(
        tournament=tournament,
        stage=_pool_stage(tournament),
        name=name,
        sort_order=next_order,
    )
    TournamentPoolEntry.objects.bulk_create([
        TournamentPoolEntry(pool=pool, team=team, seed_order=index)
        for index, team in enumerate(teams, start=1)
    ])
    return pool


@transaction.atomic
def update_pool(
    tournament: Tournament,
    pool: TournamentPool,
    *,
    name: str,
    team_ids: list[UUID],
) -> TournamentPool:
    """Replace a draft pool's label and ordered team assignment.

    Raises:
        TournamentEditingError: If teams are unavailable or pools are locked.

    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    pool = tournament.pools.select_for_update().get(pk=pool.pk)
    _ensure_pools_editable(tournament)
    if tournament.pools.filter(name__iexact=name).exclude(pk=pool.pk).exists():
        raise TournamentEditingError("A pool with this name already exists.")
    teams = _pool_teams(tournament, team_ids, exclude_pool=pool)
    pool.name = name
    pool.save(update_fields=["name"])
    pool.entries.all().delete()
    TournamentPoolEntry.objects.bulk_create([
        TournamentPoolEntry(pool=pool, team=team, seed_order=index)
        for index, team in enumerate(teams, start=1)
    ])
    return pool


@transaction.atomic
def delete_pool(tournament: Tournament, pool: TournamentPool) -> None:
    """Delete one pool while no generated or manual matches exist."""
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    pool = tournament.pools.select_for_update().get(pk=pool.pk)
    _ensure_pools_editable(tournament)
    stage = pool.stage
    pool.delete()
    if not stage.pools.exists():
        stage.delete()


def _editable_match(match: TournamentMatch) -> None:
    if (
        match.status != TournamentMatch.Status.SCHEDULED
        or match.home_score is not None
        or match.away_score is not None
    ):
        raise TournamentEditingError(
            "Only unstarted matches without a saved score can be edited."
        )


def _match_objects(
    tournament: Tournament,
    *,
    pool_id: UUID,
    home_team_id: UUID,
    away_team_id: UUID,
    field_id: UUID,
) -> tuple[TournamentPool, TournamentTeam, TournamentTeam, TournamentField]:
    if home_team_id == away_team_id:
        raise TournamentEditingError("A team cannot play against itself.")
    try:
        pool = tournament.pools.get(pk=pool_id, stage__kind=TournamentStage.Kind.POOL)
        home = tournament.teams.get(pk=home_team_id, withdrawn=False)
        away = tournament.teams.get(pk=away_team_id, withdrawn=False)
        field = tournament.fields.get(pk=field_id, active=True)
    except (
        TournamentPool.DoesNotExist,
        TournamentTeam.DoesNotExist,
        TournamentField.DoesNotExist,
    ) as exc:
        raise TournamentEditingError(
            "Select a pool, active teams, and an active field from this tournament."
        ) from exc
    pool_team_ids = set(pool.entries.values_list("team_id", flat=True))
    if home.pk not in pool_team_ids or away.pk not in pool_team_ids:
        raise TournamentEditingError("Both teams must belong to the selected pool.")
    return pool, home, away, field


def _ensure_available(
    tournament: Tournament,
    *,
    draft: _ResolvedMatchDraft,
    exclude_match: TournamentMatch | None = None,
) -> None:
    ends_at = draft.starts_at + timedelta(minutes=draft.duration_minutes)
    candidates = tournament.matches.filter(starts_at__isnull=False).exclude(
        status=TournamentMatch.Status.CANCELLED
    )
    if exclude_match:
        candidates = candidates.exclude(pk=exclude_match.pk)
    candidates = candidates.filter(
        Q(field=draft.field)
        | Q(home_team__in=(draft.home, draft.away))
        | Q(away_team__in=(draft.home, draft.away))
    )
    for other in candidates:
        if other.starts_at is None:
            continue
        other_end = other.starts_at + timedelta(minutes=other.duration_minutes)
        if draft.starts_at < other_end and other.starts_at < ends_at:
            raise TournamentEditingError(
                f"This time overlaps with match {other.match_number} "
                "for a team or field."
            )


@transaction.atomic
def save_match(
    tournament: Tournament,
    *,
    draft: MatchDraft,
    match: TournamentMatch | None = None,
) -> TournamentMatch:
    """Create or replace one conflict-free draft match."""
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    if match:
        match = tournament.matches.select_for_update().get(pk=match.pk)
        _editable_match(match)
    pool, home, away, field = _match_objects(
        tournament,
        pool_id=draft.pool_id,
        home_team_id=draft.home_team_id,
        away_team_id=draft.away_team_id,
        field_id=draft.field_id,
    )
    _ensure_available(
        tournament,
        draft=_ResolvedMatchDraft(
            starts_at=draft.starts_at,
            duration_minutes=draft.duration_minutes,
            field=field,
            home=home,
            away=away,
        ),
        exclude_match=match,
    )
    if match is None:
        match_number = (
            tournament.matches.aggregate(value=Max("match_number"))["value"] or 0
        ) + 1
        match = TournamentMatch(tournament=tournament, match_number=match_number)
    match.stage = pool.stage
    match.pool = pool
    match.home_team = home
    match.away_team = away
    match.field = field
    match.starts_at = draft.starts_at
    match.duration_minutes = draft.duration_minutes
    match.round_number = draft.round_number
    match.save()
    return match


@transaction.atomic
def delete_match(tournament: Tournament, match: TournamentMatch) -> None:
    """Delete one unstarted and unscored match."""
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    match = tournament.matches.select_for_update().get(pk=match.pk)
    _editable_match(match)
    match.delete()
