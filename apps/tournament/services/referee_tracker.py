"""Focused tournament referee state and scoring commands."""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from apps.tournament.models import (
    Tournament,
    TournamentMatch,
    TournamentResultAudit,
)


MAX_TOURNAMENT_SCORE = 999


class RefereeTrackerError(Exception):
    """A requested referee action is incompatible with current match state."""


def build_referee_tracker_state(match: TournamentMatch) -> dict[str, Any]:
    """Return only the match data needed by the field referee."""
    return {
        "tournament": {
            "id_uuid": str(match.tournament_id),
            "name": match.tournament.name,
            "slug": match.tournament.slug,
        },
        "match": {
            "id_uuid": str(match.id_uuid),
            "match_number": match.match_number,
            "status": match.status,
            "field_ready_at": (
                match.field_ready_at.isoformat() if match.field_ready_at else None
            ),
            "field": (
                {"id_uuid": str(match.field_id), "label": match.field.label}
                if match.field
                else None
            ),
            "home_team": (
                {
                    "id_uuid": str(match.home_team_id),
                    "name": match.home_team.name,
                    "short_name": match.home_team.short_name,
                }
                if match.home_team
                else None
            ),
            "away_team": (
                {
                    "id_uuid": str(match.away_team_id),
                    "name": match.away_team.name,
                    "short_name": match.away_team.short_name,
                }
                if match.away_team
                else None
            ),
            "home_score": match.home_score,
            "away_score": match.away_score,
            "revision": match.revision,
        },
    }


def mark_field_ready(match: TournamentMatch, *, actor: object) -> None:
    """Record a field's one-way readiness signal for this fixture.

    Raises:
        RefereeTrackerError: If the fixture cannot be readied.

    """
    if match.status in {
        TournamentMatch.Status.FINAL,
        TournamentMatch.Status.CANCELLED,
    }:
        raise RefereeTrackerError(
            "Een afgeronde wedstrijd kan niet gereed worden gemeld."
        )
    if match.home_team is None or match.away_team is None:
        raise RefereeTrackerError(
            "Beide teams moeten bekend zijn voordat het veld gereed is."
        )
    if match.field_ready_at is not None:
        return

    match.field_ready_at = timezone.now()
    match.field_ready_by = actor
    match.revision += 1
    match.save(
        update_fields=[
            "field_ready_at",
            "field_ready_by",
            "revision",
            "updated_at",
        ]
    )


def record_goal(
    match: TournamentMatch,
    *,
    side: str,
    actor: object,
) -> None:
    """Increment one team score and keep the direct-result audit complete.

    Raises:
        RefereeTrackerError: If the fixture cannot accept the goal.

    """
    if match.status in {
        TournamentMatch.Status.FINAL,
        TournamentMatch.Status.CANCELLED,
    }:
        raise RefereeTrackerError("Deze wedstrijd accepteert geen doelpunten meer.")
    if match.field_ready_at is None:
        raise RefereeTrackerError("Meld het veld gereed voordat je doelpunten invoert.")
    if match.home_team is None or match.away_team is None:
        raise RefereeTrackerError(
            "Beide teams moeten bekend zijn voor de score kan starten."
        )

    previous_home_score = match.home_score
    previous_away_score = match.away_score
    previous_status = match.status
    home_score = match.home_score or 0
    away_score = match.away_score or 0
    if side == "home":
        if home_score >= MAX_TOURNAMENT_SCORE:
            raise RefereeTrackerError("De thuisstand kan niet verder worden verhoogd.")
        home_score += 1
    elif side == "away":
        if away_score >= MAX_TOURNAMENT_SCORE:
            raise RefereeTrackerError("De uitstand kan niet verder worden verhoogd.")
        away_score += 1
    else:
        raise RefereeTrackerError("Kies het thuis- of uitteam.")

    new_status = (
        TournamentMatch.Status.LIVE
        if match.status == TournamentMatch.Status.SCHEDULED
        else match.status
    )
    TournamentResultAudit.objects.create(
        match=match,
        previous_home_score=previous_home_score,
        previous_away_score=previous_away_score,
        new_home_score=home_score,
        new_away_score=away_score,
        previous_status=previous_status,
        new_status=new_status,
        reason="Goal recorded by referee tracker",
        changed_by=actor,
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
    if match.tournament.status == Tournament.Status.PUBLISHED:
        match.tournament.status = Tournament.Status.LIVE
        match.tournament.save(update_fields=["status", "updated_at"])
