"""Operational commands for centrally managed tournament matches."""

from __future__ import annotations

from collections.abc import Sequence

from django.utils import timezone

from apps.tournament.models import TournamentMatch, TournamentResultAudit


class TournamentMatchOperationError(ValueError):
    """Raised when a live-operation command is not valid for the current state."""


def downstream_result_locked(match: TournamentMatch, winner: object | None) -> bool:
    """Return whether changing an advanced team would invalidate played data."""
    if not match.next_match or match.winner_id == getattr(winner, "pk", None):
        return False
    destination = match.next_match
    return (
        destination.status != TournamentMatch.Status.SCHEDULED
        or destination.home_score is not None
        or destination.away_score is not None
    )


def sync_advanced_winner(match: TournamentMatch) -> None:
    """Mirror a final winner into its configured downstream bracket slot."""
    if not match.next_match or not match.winner_to_side:
        return
    destination = match.next_match
    replacement = match.winner if match.status == TournamentMatch.Status.FINAL else None
    if match.winner_to_side == TournamentMatch.DestinationSide.HOME:
        destination.home_team = replacement
        update_field = "home_team"
    else:
        destination.away_team = replacement
        update_field = "away_team"
    destination.save(update_fields=[update_field, "updated_at"])


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


def reset_match_state(match: TournamentMatch, *, actor: object) -> bool:
    """Move one match back by one lifecycle step.

    Scheduled matches reset their field-readiness signal. Live and cancelled
    matches return to a clean scheduled state. Final matches reopen as live so
    their recorded result remains available for correction.

    Raises:
        TournamentMatchOperationError: If resetting would invalidate a played
            downstream bracket match.

    """
    if match.status == TournamentMatch.Status.SCHEDULED:
        return reset_field_readiness(match)

    previous_status = match.status
    previous_home_score = match.home_score
    previous_away_score = match.away_score
    if previous_status == TournamentMatch.Status.FINAL:
        new_status = TournamentMatch.Status.LIVE
        home_score = match.home_score
        away_score = match.away_score
    else:
        new_status = TournamentMatch.Status.SCHEDULED
        home_score = None
        away_score = None

    if downstream_result_locked(match, None):
        raise TournamentMatchOperationError(
            "De volgende wedstrijd is al gestart. Zet die eerst terug."
        )

    TournamentResultAudit.objects.create(
        match=match,
        previous_home_score=previous_home_score,
        previous_away_score=previous_away_score,
        new_home_score=home_score,
        new_away_score=away_score,
        previous_status=previous_status,
        new_status=new_status,
        reason="Match state reset by tournament manager",
        changed_by=actor,
        changed_by_name=str(actor),
        source=TournamentResultAudit.Source.DIRECT,
    )
    match.home_score = home_score
    match.away_score = away_score
    match.status = new_status
    match.winner = None
    match.revision += 1
    match.save(
        update_fields=[
            "home_score",
            "away_score",
            "status",
            "winner",
            "revision",
            "updated_at",
        ]
    )
    sync_advanced_winner(match)
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
