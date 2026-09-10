"""Edit explicit cup rounds and progression without inferring provider brackets."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from apps.tournament.models import TournamentMatch, TournamentStage
from apps.tournament.services.cups import CupError
from apps.tournament.services.match_operations import replace_scheduled_match_teams


@dataclass(frozen=True)
class CupMatchPlan:
    """Organizer-confirmed schedule and winner destination."""

    round_name: str
    round_number: int
    starts_at: datetime
    field_id: UUID
    next_match_id: UUID | None
    winner_to_side: str
    replace_destination_team: bool = False
    expected_destination_revision: int | None = None


def plan_cup_match(match: TournamentMatch, plan: CupMatchPlan) -> None:
    """Update one unstarted fixture under the caller's aggregate lock.

    Raises:
        CupError: If a link is cyclic, occupied, foreign or has already started.

    """
    tournament = match.tournament
    if (
        not tournament.cup_rules
        or match.status != TournamentMatch.Status.SCHEDULED
        or match.field_ready_at
    ):
        raise CupError(
            "Pas alleen een geplande, nog niet gereed gemelde bekerwedstrijd aan."
        )
    field = tournament.fields.filter(pk=plan.field_id, active=True).first()
    if field is None:
        raise CupError("Kies een actief veld uit deze beker.")
    destination = None
    if plan.next_match_id:
        destination = (
            tournament.matches
            .select_for_update(of=("self",))
            .filter(
                pk=plan.next_match_id,
                status=TournamentMatch.Status.SCHEDULED,
                field_ready_at__isnull=True,
            )
            .first()
        )
        if destination is None or plan.winner_to_side not in {"home", "away"}:
            raise CupError(
                "Kies een geplande vervolgw wedstrijd en de thuis- of uitplek."
            )
        _validate_destination_slot(match, destination, plan)
        current = destination
        seen = {match.pk}
        while current:
            if current.pk in seen:
                raise CupError("Doorstroming mag geen cirkel vormen.")
            seen.add(current.pk)
            current = current.next_match
    stage, _ = TournamentStage.objects.get_or_create(
        tournament=tournament,
        name=plan.round_name,
        defaults={
            "kind": TournamentStage.Kind.KNOCKOUT,
            "sort_order": plan.round_number,
        },
    )
    if stage.kind == TournamentStage.Kind.POOL:
        raise CupError("Kies een bekerfase in plaats van een poulefase.")
    if destination and getattr(destination, f"{plan.winner_to_side}_team_id"):
        replace_scheduled_match_teams(
            destination, {f"{plan.winner_to_side}_team": None}
        )
    match.stage = stage
    match.round_number = plan.round_number
    match.starts_at = plan.starts_at
    match.field = field
    match.next_match = destination
    match.winner_to_side = plan.winner_to_side if destination else ""
    match.revision += 1
    match.save(
        update_fields=[
            "stage",
            "round_number",
            "starts_at",
            "field",
            "next_match",
            "winner_to_side",
            "revision",
            "updated_at",
        ]
    )


def _validate_destination_slot(
    match: TournamentMatch, destination: TournamentMatch, plan: CupMatchPlan
) -> None:
    """Require explicit replacement of an eligible, unchanged bracket entrant.

    Raises:
        CupError: If the slot is occupied, stale or would duplicate a team.

    """
    if (
        match.tournament.matches
        .filter(next_match=destination, winner_to_side=plan.winner_to_side)
        .exclude(pk=match.pk)
        .exists()
    ):
        raise CupError("Deze plek in de volgende wedstrijd is al bezet.")
    occupant = getattr(destination, f"{plan.winner_to_side}_team_id")
    if occupant and (
        not plan.replace_destination_team
        or plan.expected_destination_revision != destination.revision
        or occupant not in {match.home_team_id, match.away_team_id}
    ):
        raise CupError(
            "Bevestig het vervangen van een deelnemend team "
            "met de nieuwste wedstrijdplanning."
        )
    other_side = "away" if plan.winner_to_side == "home" else "home"
    other_team = getattr(destination, f"{other_side}_team_id")
    if other_team and other_team in {match.home_team_id, match.away_team_id}:
        raise CupError("Een mogelijke winnaar staat al aan de andere kant.")
