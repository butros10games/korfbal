"""Generate a single-elimination stage from finalized pool standings."""

from __future__ import annotations

from datetime import datetime, timedelta
import math

from django.db import transaction
from django.db.models import Max

from apps.tournament.models import (
    Tournament,
    TournamentMatch,
    TournamentStage,
    TournamentTeam,
)
from apps.tournament.services.generation import GenerationError
from apps.tournament.services.standings import calculate_pool_standings


MIN_FINALISTS = 2


def _is_power_of_two(value: int) -> bool:
    return value >= MIN_FINALISTS and value & (value - 1) == 0


def _qualified_teams(
    tournament: Tournament, qualifiers_per_pool: int
) -> list[TournamentTeam]:
    pools = list(tournament.pools.select_related("stage").order_by("sort_order"))
    if not pools:
        raise GenerationError("Generate pool play before creating finals.")
    incomplete = tournament.matches.filter(
        stage__kind=TournamentStage.Kind.POOL
    ).exclude(
        status__in=[TournamentMatch.Status.FINAL, TournamentMatch.Status.CANCELLED]
    )
    if incomplete.exists():
        raise GenerationError("Finalize all pool matches before creating finals.")

    ranked_ids: list[str] = []
    standings = [calculate_pool_standings(pool) for pool in pools]
    for rank in range(qualifiers_per_pool):
        for pool_rows in standings:
            if rank < len(pool_rows):
                ranked_ids.append(pool_rows[rank]["team_id"])
    if not _is_power_of_two(len(ranked_ids)):
        raise GenerationError(
            "The qualifier count must produce 2, 4, 8, or 16 finalists."
        )
    lookup = {
        str(team.id_uuid): team
        for team in tournament.teams.filter(id_uuid__in=ranked_ids)
    }
    return [lookup[team_id] for team_id in ranked_ids]


def _finals_start(tournament: Tournament, requested: datetime | None) -> datetime:
    if requested:
        return requested
    last_match = (
        tournament.matches
        .exclude(starts_at__isnull=True)
        .order_by("-starts_at")
        .first()
    )
    if not last_match or not last_match.starts_at:
        return tournament.starts_at
    return last_match.starts_at + timedelta(
        minutes=last_match.duration_minutes
        + tournament.changeover_minutes
        + tournament.minimum_rest_minutes
    )


@transaction.atomic
def generate_finals(
    tournament: Tournament,
    *,
    qualifiers_per_pool: int,
    starts_at: datetime | None = None,
) -> TournamentStage:
    """Create and wire a power-of-two single-elimination bracket.

    Raises:
        GenerationError: If pool play is incomplete or qualifiers are invalid.

    """
    if qualifiers_per_pool < 1:
        raise GenerationError("At least one team per pool must qualify.")
    if tournament.stages.filter(
        kind__in=[TournamentStage.Kind.KNOCKOUT, TournamentStage.Kind.FINAL]
    ).exists():
        raise GenerationError("A knockout stage already exists.")
    fields = list(tournament.fields.filter(active=True))
    if not fields:
        raise GenerationError("Add an active field before creating finals.")
    teams = _qualified_teams(tournament, qualifiers_per_pool)
    next_stage_order = (
        tournament.stages.aggregate(value=Max("sort_order"))["value"] or 0
    ) + 1
    next_number = (
        tournament.matches.aggregate(value=Max("match_number"))["value"] or 0
    ) + 1
    round_count = int(math.log2(len(teams)))
    knockout_stage = None
    if round_count > 1:
        knockout_stage = TournamentStage.objects.create(
            tournament=tournament,
            name="Knockout",
            kind=TournamentStage.Kind.KNOCKOUT,
            sort_order=next_stage_order,
            qualifiers_per_pool=qualifiers_per_pool,
        )
        next_stage_order += 1
    final_stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Finale",
        kind=TournamentStage.Kind.FINAL,
        sort_order=next_stage_order,
        qualifiers_per_pool=qualifiers_per_pool,
    )
    current_start = _finals_start(tournament, starts_at)
    rounds: list[list[TournamentMatch]] = []

    for round_index in range(round_count):
        match_stage = final_stage if round_index == round_count - 1 else knockout_stage
        matches_in_round = len(teams) // (2 ** (round_index + 1))
        round_matches: list[TournamentMatch] = []
        for position in range(matches_in_round):
            home_team = None
            away_team = None
            if round_index == 0:
                home_team = teams[position]
                away_team = teams[-1 - position]
            slot = position // len(fields)
            field = fields[position % len(fields)]
            match = TournamentMatch.objects.create(
                tournament=tournament,
                stage=match_stage,
                home_team=home_team,
                away_team=away_team,
                field=field,
                round_number=round_index + 1,
                match_number=next_number,
                starts_at=current_start
                + timedelta(
                    minutes=slot
                    * (
                        tournament.match_duration_minutes
                        + tournament.changeover_minutes
                    )
                ),
                duration_minutes=tournament.match_duration_minutes,
            )
            next_number += 1
            round_matches.append(match)
        rounds.append(round_matches)
        slots = math.ceil(matches_in_round / len(fields))
        current_start += timedelta(
            minutes=slots
            * (tournament.match_duration_minutes + tournament.changeover_minutes)
            + tournament.minimum_rest_minutes
        )

    for round_index, round_matches in enumerate(rounds[:-1]):
        next_round = rounds[round_index + 1]
        for position, match in enumerate(round_matches):
            match.next_match = next_round[position // 2]
            match.winner_to_side = (
                TournamentMatch.DestinationSide.HOME
                if position % 2 == 0
                else TournamentMatch.DestinationSide.AWAY
            )
            match.save(update_fields=["next_match", "winner_to_side", "updated_at"])
    return final_stage
