"""Seed a direct knockout cup, including byes, using native tournament matches."""

from datetime import timedelta

from apps.tournament.models import (
    Tournament,
    TournamentMatch,
    TournamentStage,
    TournamentTeam,
)
from apps.tournament.services.cups import CupError


MIN_TEAMS = 2
SEMIFINAL_TEAMS = 4
MAX_TEAMS = 128


def generate_cup_draw(tournament: Tournament) -> None:
    """Create a draw once; caller holds the tournament lock and transaction.

    Raises:
        CupError: If rules, participants or fields are missing, or play already exists.

    """
    if not tournament.cup_rules:
        raise CupError("Stel eerst de bekerregels in.")
    if tournament.matches.exists() or tournament.stages.exists():
        raise CupError("Maak de bekerindeling in een leeg toernooi.")
    teams = list(
        tournament.teams.filter(withdrawn=False).order_by(
            "seed", "sort_order", "id_uuid"
        )
    )
    fields = list(tournament.fields.filter(active=True))
    if not MIN_TEAMS <= len(teams) <= MAX_TEAMS or not fields:
        raise CupError("Voeg 2 tot 128 teams en minimaal één actief veld toe.")
    size = 1 << (len(teams) - 1).bit_length()
    seeds = [1, 2]
    while len(seeds) < size:
        seeds = [value for seed in seeds for value in (seed, 2 * len(seeds) + 1 - seed)]
    nodes: list[TournamentTeam | TournamentMatch | None] = [
        teams[seed - 1] if seed <= len(teams) else None for seed in seeds
    ]
    round_number = 1
    number = 1
    duration = sum(tournament.cup_rules["regular_minutes"])
    slot_minutes = (
        duration
        + sum(tournament.cup_rules["extra_minutes"])
        + tournament.changeover_minutes
    )
    starts_at = tournament.starts_at
    stages: list[TournamentStage] = []
    matches: list[TournamentMatch] = []
    while len(nodes) > 1:
        name = (
            "Finale"
            if len(nodes) == MIN_TEAMS
            else (
                "Halve finale"
                if len(nodes) == SEMIFINAL_TEAMS
                else f"Laatste {len(nodes)}"
            )
        )
        stage = TournamentStage(
            tournament=tournament,
            name=name,
            kind=TournamentStage.Kind.FINAL
            if len(nodes) == MIN_TEAMS
            else TournamentStage.Kind.KNOCKOUT,
            sort_order=round_number,
        )
        stages.append(stage)
        next_nodes: list[TournamentTeam | TournamentMatch | None] = []
        scheduled = 0
        for index in range(0, len(nodes), 2):
            home, away = nodes[index : index + 2]
            if home is None or away is None:
                next_nodes.append(home or away)
                continue
            match = TournamentMatch(
                tournament=tournament,
                stage=stage,
                round_number=round_number,
                match_number=number,
                home_team=home if isinstance(home, TournamentTeam) else None,
                away_team=away if isinstance(away, TournamentTeam) else None,
                field=fields[scheduled % len(fields)],
                duration_minutes=duration,
                starts_at=starts_at
                + timedelta(minutes=(scheduled // len(fields)) * slot_minutes),
            )
            for node, side in ((home, "home"), (away, "away")):
                if isinstance(node, TournamentMatch):
                    node.next_match, node.winner_to_side = match, side
            matches.append(match)
            next_nodes.append(match)
            number += 1
            scheduled += 1
        nodes = next_nodes
        starts_at += timedelta(
            minutes=((scheduled + len(fields) - 1) // len(fields)) * slot_minutes
            + tournament.minimum_rest_minutes
        )
        round_number += 1

    TournamentStage.objects.bulk_create(stages)
    # UUIDs are allocated in memory. Insert later rounds first so each feeder's
    # destination already exists, including across database-sized insert batches.
    TournamentMatch.objects.bulk_create(list(reversed(matches)))
