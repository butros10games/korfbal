"""Plan a single-elimination stage backed by automatic pool qualifiers."""

from __future__ import annotations

from datetime import datetime, timedelta
import math

from django.db import transaction
from django.db.models import Max

from apps.tournament.models import (
    Tournament,
    TournamentMatch,
    TournamentStage,
)
from apps.tournament.services.final_groups import resolve_tournament_qualifiers
from apps.tournament.services.generation import GenerationError


MIN_FINALISTS = 2


def _is_power_of_two(value: int) -> bool:
    return value >= MIN_FINALISTS and value & (value - 1) == 0


def _qualifier_sources(
    tournament: Tournament, qualifiers_per_pool: int
) -> list[dict[str, object]]:
    pools = list(
        tournament.pools
        .select_related("stage")
        .prefetch_related("entries", "matches")
        .order_by("sort_order")
    )
    if not pools:
        raise GenerationError("Generate pool play before creating finals.")
    if any(not list(pool.matches.all()) for pool in pools):
        raise GenerationError("Schedule every pool before creating finals.")

    sources: list[dict[str, object]] = []
    for rank in range(qualifiers_per_pool):
        for pool in pools:
            if rank < len(list(pool.entries.all())):
                sources.append({
                    "kind": "pool_rank",
                    "pool_ids": [str(pool.id_uuid)],
                    "rank": rank + 1,
                })
    if not _is_power_of_two(len(sources)):
        raise GenerationError(
            "The qualifier count must produce 2, 4, 8, or 16 finalists."
        )
    return sources


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
        GenerationError: If pool play is unscheduled or qualifiers are invalid.

    """
    if qualifiers_per_pool < 1:
        raise GenerationError("At least one team per pool must qualify.")
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    if tournament.stages.filter(
        kind__in=[TournamentStage.Kind.KNOCKOUT, TournamentStage.Kind.FINAL]
    ).exists():
        raise GenerationError("A knockout stage already exists.")
    fields = list(tournament.fields.filter(active=True))
    if not fields:
        raise GenerationError("Add an active field before creating finals.")
    sources = _qualifier_sources(tournament, qualifiers_per_pool)
    next_stage_order = (
        tournament.stages.aggregate(value=Max("sort_order"))["value"] or 0
    ) + 1
    next_number = (
        tournament.matches.aggregate(value=Max("match_number"))["value"] or 0
    ) + 1
    round_count = int(math.log2(len(sources)))
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
        matches_in_round = len(sources) // (2 ** (round_index + 1))
        round_matches: list[TournamentMatch] = []
        for position in range(matches_in_round):
            home_qualifier: dict[str, object] = {}
            away_qualifier: dict[str, object] = {}
            if round_index == 0:
                home_qualifier = sources[position]
                away_qualifier = sources[-1 - position]
            slot = position // len(fields)
            field = fields[position % len(fields)]
            match = TournamentMatch.objects.create(
                tournament=tournament,
                stage=match_stage,
                home_qualifier=home_qualifier,
                away_qualifier=away_qualifier,
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
    resolve_tournament_qualifiers(tournament)
    return final_stage
