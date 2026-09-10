"""Explicitly connect observed KNKV cups to an owned native tournament."""

from django.db import transaction

from apps.competition.models import CupCompetition, CupFixture
from apps.tournament.models import (
    Tournament,
    TournamentMatch,
    TournamentStage,
    TournamentTeam,
)
from apps.tournament.services.cups import CupError, validate_cup_rules


@transaction.atomic
def publish_cup(cup_id: int, tournament_id: str) -> int:
    """Populate an empty owned event; never infer unknown rounds or overwrite play.

    The original competition Match remains the source of official KNKV results.
    The linked tournament fixture records local operational play independently.

    Raises:
        CupError: If adoption would mix cups, overwrite play or use unknown rules.

    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament_id)
    cup = CupCompetition.objects.select_for_update().get(pk=cup_id)
    if cup.local_tournament_id == tournament.pk:
        return 0
    if (
        cup.local_tournament_id
        or tournament.matches.exists()
        or tournament.stages.exists()
        or tournament.teams.exists()
    ):
        raise CupError("Koppel de bronbeker aan een leeg, eigen toernooi.")
    validate_cup_rules(tournament.cup_rules)
    fixtures = list(
        CupFixture.objects
        .filter(competition=cup)
        .select_related("match", "match__home_team__group", "match__away_team__group")
        .order_by("match__starts_at", "match__external_id")
    )
    if not fixtures:
        raise CupError("Deze bronbeker heeft nog geen wedstrijden.")
    teams = {}
    native_teams = {}
    stages = {}
    matches = []
    for number, fixture in enumerate(fixtures, 1):
        source = fixture.match
        for team in (source.home_team, source.away_team):
            identity = (
                ("group", team.group_id) if team.group_id else ("source", team.pk)
            )
            if identity not in native_teams:
                native_teams[identity] = TournamentTeam(
                    tournament=tournament,
                    name=team.name,
                    linked_team_id=team.group.local_team_id if team.group_id else None,
                )
            teams[team.pk] = native_teams[identity]
        label = fixture.round_name or "Ronde onbekend"
        if label not in stages:
            stages[label] = TournamentStage(
                tournament=tournament, name=label, kind=TournamentStage.Kind.KNOCKOUT
            )
        closed = source.status == "FINAL"
        state = {}
        if closed:
            state = {
                "rules": tournament.cup_rules,
                "phase": "unobserved",
                "period": 0,
                "completed_periods": [],
                "attempts": [],
                "shootout_first": None,
            }
        local = TournamentMatch(
            tournament=tournament,
            stage=stages[label],
            round_number=fixture.round_number,
            home_team=teams[source.home_team_id],
            away_team=teams[source.away_team_id],
            match_number=number,
            starts_at=source.starts_at,
            duration_minutes=source.playing_time_minutes
            or tournament.match_duration_minutes,
            status=TournamentMatch.Status.FINAL
            if closed
            else TournamentMatch.Status.SCHEDULED,
            home_score=source.home_score if closed else None,
            away_score=source.away_score if closed else None,
            cup_state=state,
        )
        # A level official score does not establish the winner of a penalty series.
        if (
            closed
            and source.home_score is not None
            and source.away_score is not None
            and source.home_score != source.away_score
        ):
            local.winner = (
                local.home_team
                if source.home_score > source.away_score
                else local.away_team
            )
        matches.append(local)
        fixture.local_match = local
    TournamentTeam.objects.bulk_create(list(native_teams.values()))
    TournamentStage.objects.bulk_create(list(stages.values()))
    TournamentMatch.objects.bulk_create(matches)
    CupFixture.objects.bulk_update(fixtures, ["local_match"], batch_size=500)
    cup.local_tournament = tournament
    cup.save(update_fields=["local_tournament"])
    return len(fixtures)
