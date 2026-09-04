"""Operational commands for centrally managed tournament matches."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from django.utils import timezone

from apps.tournament.models import (
    TournamentMatch,
    TournamentResultAudit,
    TournamentTeam,
)


class TournamentMatchOperationError(ValueError):
    """Raised when a live-operation command is not valid for the current state."""


def replace_scheduled_match_teams(
    match: TournamentMatch,
    replacements: Mapping[str, TournamentTeam | None],
) -> bool:
    """Replace bracket entrants and invalidate readiness tied to old entrants.

    Raises:
        TournamentMatchOperationError: If both replacements resolve to one team.

    """
    changed_fields = [
        field
        for field, replacement in replacements.items()
        if getattr(match, f"{field}_id") != getattr(replacement, "pk", None)
    ]
    if not changed_fields:
        return False

    prospective = {
        "home_team": replacements.get("home_team", match.home_team),
        "away_team": replacements.get("away_team", match.away_team),
    }
    home_id = getattr(prospective["home_team"], "pk", None)
    away_id = getattr(prospective["away_team"], "pk", None)
    if home_id is not None and home_id == away_id:
        raise TournamentMatchOperationError(
            "A qualifier would place one team on both sides."
        )

    for field in changed_fields:
        setattr(match, field, replacements[field])
    update_fields = list(changed_fields)
    if match.field_ready_at is not None:
        match.field_ready_at = None
        match.field_ready_by = None
        match.field_ready_by_name = ""
        update_fields.extend([
            "field_ready_at",
            "field_ready_by",
            "field_ready_by_name",
        ])
    if match.referee_team_id is not None and match.referee_team_id in {
        home_id,
        away_id,
    }:
        match.referee_team = None
        match.referee_name = ""
        match.referee_player = None
        match.referee_claim_token = None
        match.referee_claimed_at = None
        update_fields.extend([
            "referee_team",
            "referee_name",
            "referee_player",
            "referee_claim_token",
            "referee_claimed_at",
        ])
    match.revision += 1
    update_fields.extend(["revision", "updated_at"])
    match.save(update_fields=update_fields)
    return True


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
        replacements = {"home_team": replacement}
    else:
        replacements = {"away_team": replacement}
    replace_scheduled_match_teams(destination, replacements)


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
