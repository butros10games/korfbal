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
from apps.tournament.services.referee_tracker import (
    RefereeTrackerError,
    assign_referee_team,
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
class MatchSubstitution:
    """One guest-team assignment for an absent team's match."""

    match_id: UUID
    substitute_team_id: UUID


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


def _pool_field(
    tournament: Tournament,
    assigned_field_id: UUID | None,
) -> TournamentField | None:
    if assigned_field_id is None:
        return None
    try:
        return tournament.fields.get(pk=assigned_field_id, active=True)
    except TournamentField.DoesNotExist as exc:
        raise TournamentEditingError(
            "Select an active field from this tournament."
        ) from exc


@transaction.atomic
def create_pool(
    tournament: Tournament,
    *,
    name: str,
    team_ids: list[UUID],
    assigned_field_id: UUID | None = None,
    sort_order: int | None = None,
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
    assigned_field = _pool_field(tournament, assigned_field_id)
    next_order = (
        sort_order
        if sort_order is not None
        else (tournament.pools.aggregate(value=Max("sort_order"))["value"] or 0) + 1
    )
    pool = TournamentPool.objects.create(
        tournament=tournament,
        stage=_pool_stage(tournament),
        assigned_field=assigned_field,
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
    assigned_field_id: UUID | None = None,
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
    assigned_field = _pool_field(tournament, assigned_field_id)
    pool.name = name
    pool.assigned_field = assigned_field
    pool.save(update_fields=["name", "assigned_field"])
    pool.entries.all().delete()
    TournamentPoolEntry.objects.bulk_create([
        TournamentPoolEntry(pool=pool, team=team, seed_order=index)
        for index, team in enumerate(teams, start=1)
    ])
    return pool


@transaction.atomic
def update_pool_order(
    tournament: Tournament,
    pool: TournamentPool,
    *,
    sort_order: int,
) -> TournamentPool:
    """Change presentation order without invalidating an existing schedule."""
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    pool = tournament.pools.select_for_update().get(pk=pool.pk)
    pool.sort_order = sort_order
    pool.save(update_fields=["sort_order"])
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
    if pool.assigned_field_id and pool.assigned_field_id != field.pk:
        raise TournamentEditingError(
            f'Pool "{pool.name}" is assigned to field "{pool.assigned_field.label}".'
        )
    return pool, home, away, field


def _validate_match_teams(
    pool: TournamentPool,
    home: TournamentTeam,
    away: TournamentTeam,
    *,
    allow_guest_team: bool,
) -> None:
    pool_team_ids = set(pool.entries.values_list("team_id", flat=True))
    outside_pool = [team for team in (home, away) if team.pk not in pool_team_ids]
    if outside_pool and not allow_guest_team:
        raise TournamentEditingError("Both teams must belong to the selected pool.")
    if len(outside_pool) > 1:
        raise TournamentEditingError(
            "At least one team must belong to the selected pool."
        )
    if outside_pool and not outside_pool[0].pool_entries.exclude(pool=pool).exists():
        raise TournamentEditingError("The guest team must belong to another pool.")


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
    allow_guest_team = False
    if match is not None and match.pool_id == pool.pk:
        pool_team_ids = set(pool.entries.values_list("team_id", flat=True))
        allow_guest_team = (
            match.home_team_id not in pool_team_ids
            or match.away_team_id not in pool_team_ids
        )
    _validate_match_teams(
        pool,
        home,
        away,
        allow_guest_team=allow_guest_team,
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


@transaction.atomic
def substitute_absent_team(
    tournament: Tournament,
    *,
    absent_team_id: UUID,
    substitutions: list[MatchSubstitution],
    referee_substitutions: list[MatchSubstitution],
) -> list[TournamentMatch]:
    """Fill every remaining match and referee duty for an absent team.

    The actual guest participant is stored on the match while the match keeps its
    original pool. Standings deliberately ignore matches with a participant from
    outside that pool.

    Raises:
        TournamentEditingError: If the plan is incomplete, unsafe, or conflicts.

    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    try:
        absent_team = tournament.teams.select_for_update().get(pk=absent_team_id)
    except TournamentTeam.DoesNotExist as exc:
        raise TournamentEditingError("Select a team from this tournament.") from exc

    editable_matches = list(
        tournament.matches
        .select_for_update()
        .select_related("stage", "pool", "field", "home_team", "away_team")
        .filter(
            stage__kind=TournamentStage.Kind.POOL,
            pool__isnull=False,
        )
        .filter(Q(home_team=absent_team) | Q(away_team=absent_team))
        .filter(
            status=TournamentMatch.Status.SCHEDULED,
            home_score__isnull=True,
            away_score__isnull=True,
        )
    )
    editable_by_id = {match.id_uuid: match for match in editable_matches}
    requested_ids = {substitution.match_id for substitution in substitutions}
    if requested_ids != set(editable_by_id):
        raise TournamentEditingError(
            "Choose a replacement for every unstarted pool match of the absent team."
        )

    referee_matches = list(
        tournament.matches
        .select_for_update()
        .select_related("field", "home_team", "away_team", "referee_team")
        .filter(referee_team=absent_team)
        .exclude(
            status__in=(
                TournamentMatch.Status.FINAL,
                TournamentMatch.Status.CANCELLED,
            )
        )
    )
    referee_by_id = {match.id_uuid: match for match in referee_matches}
    requested_referee_ids = {
        substitution.match_id for substitution in referee_substitutions
    }
    if requested_referee_ids != set(referee_by_id):
        raise TournamentEditingError(
            "Choose a replacement for every open referee duty of the absent team."
        )
    if not editable_by_id and not referee_by_id:
        raise TournamentEditingError(
            "This team has no unstarted pool matches or open referee duties to replace."
        )

    substitute_ids = {
        item.substitute_team_id for item in (*substitutions, *referee_substitutions)
    }
    substitutes = {
        team.id_uuid: team
        for team in tournament.teams.select_for_update().filter(
            id_uuid__in=substitute_ids,
            withdrawn=False,
        )
    }
    if len(substitutes) != len(substitute_ids):
        raise TournamentEditingError("Select active teams from this tournament.")

    updated = [
        _apply_match_substitution(
            tournament,
            absent_team=absent_team,
            match=editable_by_id[substitution.match_id],
            substitute=substitutes[substitution.substitute_team_id],
        )
        for substitution in substitutions
    ]
    updated.extend(
        _apply_referee_substitution(
            tournament,
            absent_team=absent_team,
            match=referee_by_id[substitution.match_id],
            substitute=substitutes[substitution.substitute_team_id],
        )
        for substitution in referee_substitutions
    )

    absent_team.withdrawn = True
    absent_team.save(update_fields=["withdrawn"])
    return updated


def _apply_match_substitution(
    tournament: Tournament,
    *,
    absent_team: TournamentTeam,
    match: TournamentMatch,
    substitute: TournamentTeam,
) -> TournamentMatch:
    pool = match.pool
    if pool is None:
        raise TournamentEditingError("Only pool matches can use guest teams.")
    pool_team_ids = set(pool.entries.values_list("team_id", flat=True))
    if (
        substitute.pk in pool_team_ids
        or not substitute.pool_entries.exclude(pool=pool).exists()
    ):
        raise TournamentEditingError(
            f"The replacement for match {match.match_number} must come from "
            "another pool."
        )
    opponent = (
        match.away_team if match.home_team_id == absent_team.pk else match.home_team
    )
    if opponent is None or opponent.pk == substitute.pk:
        raise TournamentEditingError(
            f"Choose a different replacement for match {match.match_number}."
        )
    if match.referee_team_id == substitute.pk:
        raise TournamentEditingError(
            f"The referee for match {match.match_number} cannot also play in it."
        )
    home = substitute if match.home_team_id == absent_team.pk else opponent
    away = substitute if match.away_team_id == absent_team.pk else opponent
    _ensure_substitute_available(tournament, match=match, substitute=substitute)
    if match.starts_at is not None and match.field is not None:
        _ensure_available(
            tournament,
            draft=_ResolvedMatchDraft(
                starts_at=match.starts_at,
                duration_minutes=match.duration_minutes,
                field=match.field,
                home=home,
                away=away,
            ),
            exclude_match=match,
        )
    if match.home_team_id == absent_team.pk:
        match.home_team = substitute
    else:
        match.away_team = substitute
    match.field_ready_at = None
    match.field_ready_by = None
    match.field_ready_by_name = ""
    match.revision += 1
    match.save(
        update_fields=[
            "home_team",
            "away_team",
            "field_ready_at",
            "field_ready_by",
            "field_ready_by_name",
            "revision",
            "updated_at",
        ]
    )
    return match


def _apply_referee_substitution(
    tournament: Tournament,
    *,
    absent_team: TournamentTeam,
    match: TournamentMatch,
    substitute: TournamentTeam,
) -> TournamentMatch:
    if substitute.pk == absent_team.pk:
        raise TournamentEditingError(
            f"Choose a different referee for match {match.match_number}."
        )
    _ensure_substitute_available(tournament, match=match, substitute=substitute)
    try:
        assign_referee_team(match, team_id=substitute.id_uuid)
    except RefereeTrackerError as exc:
        raise TournamentEditingError(str(exc)) from exc
    return match


def _ensure_substitute_available(
    tournament: Tournament,
    *,
    match: TournamentMatch,
    substitute: TournamentTeam,
) -> None:
    if match.starts_at is None:
        return
    match_end = match.starts_at + timedelta(minutes=match.duration_minutes)
    other_matches = (
        tournament.matches
        .filter(starts_at__isnull=False)
        .exclude(pk=match.pk)
        .exclude(status=TournamentMatch.Status.CANCELLED)
        .filter(
            Q(home_team=substitute)
            | Q(away_team=substitute)
            | Q(referee_team=substitute)
        )
    )
    for other in other_matches:
        if other.starts_at is None:
            continue
        other_end = other.starts_at + timedelta(minutes=other.duration_minutes)
        if match.starts_at < other_end and other.starts_at < match_end:
            raise TournamentEditingError(
                f"Team {substitute.name} is already playing or refereeing "
                f"match {other.match_number} at that time."
            )
