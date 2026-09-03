"""Operational commands for centrally managed tournament matches."""

from __future__ import annotations

from collections.abc import Sequence

from django.utils import timezone

from apps.tournament.models import TournamentMatch, TournamentResultAudit


class TournamentMatchOperationError(ValueError):
    """Raised when a live-operation command is not valid for the current state."""


def reset_field_readiness(match: TournamentMatch) -> bool:
    """Revoke an incorrect pre-match readiness signal.

    Returns whether the match changed. Once play has started, readiness remains
    part of the operational history and can no longer be reset.

    Raises:
        TournamentMatchOperationError: If the match has already started.

    """
    if match.field_ready_at is None:
        return False
    if match.status != TournamentMatch.Status.SCHEDULED:
        raise TournamentMatchOperationError(
            "Een gestart of afgerond duel kan niet worden teruggezet naar niet gereed."
        )

    match.field_ready_at = None
    match.field_ready_by = None
    match.field_ready_by_name = ""
    match.revision += 1
    match.save(
        update_fields=[
            "field_ready_at",
            "field_ready_by",
            "field_ready_by_name",
            "revision",
            "updated_at",
        ]
    )
    return True


def start_round(matches: Sequence[TournamentMatch], *, actor: object) -> int:
    """Start every still-scheduled match in one round at one database instant.

    Raises:
        TournamentMatchOperationError: If no eligible match can start together.

    """
    scheduled = [
        match for match in matches if match.status == TournamentMatch.Status.SCHEDULED
    ]
    if not scheduled:
        raise TournamentMatchOperationError(
            "Deze ronde heeft geen geplande wedstrijden om te starten."
        )
    if any(
        match.home_team_id is None or match.away_team_id is None for match in scheduled
    ):
        raise TournamentMatchOperationError(
            "Alle teams moeten bekend zijn voordat de ronde kan starten."
        )
    if any(match.field_ready_at is None for match in scheduled):
        raise TournamentMatchOperationError(
            "Nog niet alle velden in deze ronde zijn gereed."
        )

    started_at = timezone.now()
    audits: list[TournamentResultAudit] = []
    for match in scheduled:
        home_score = match.home_score if match.home_score is not None else 0
        away_score = match.away_score if match.away_score is not None else 0
        audits.append(
            TournamentResultAudit(
                match=match,
                previous_home_score=match.home_score,
                previous_away_score=match.away_score,
                new_home_score=home_score,
                new_away_score=away_score,
                previous_status=match.status,
                new_status=TournamentMatch.Status.LIVE,
                reason="Round started by tournament manager",
                changed_by=actor,
                changed_by_name=str(actor),
                source=TournamentResultAudit.Source.DIRECT,
            )
        )
        match.home_score = home_score
        match.away_score = away_score
        match.status = TournamentMatch.Status.LIVE
        match.winner = None
        match.revision += 1
        match.updated_at = started_at

    TournamentResultAudit.objects.bulk_create(audits)
    TournamentMatch.objects.bulk_update(
        scheduled,
        ["home_score", "away_score", "status", "winner", "revision", "updated_at"],
    )
    return len(scheduled)
