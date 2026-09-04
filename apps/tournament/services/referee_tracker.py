"""Focused tournament referee state and scoring commands."""

from __future__ import annotations

from datetime import timedelta
from secrets import compare_digest
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from bg_uuidv7 import uuidv7
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.player.models import Player
from apps.tournament.models import (
    TournamentMatch,
    TournamentResultAudit,
    TournamentTeam,
)


MAX_TOURNAMENT_SCORE = 999


class RefereeTrackerError(Exception):
    """A requested referee action is incompatible with current match state."""


def _player_label(player: Player) -> str:
    full_name = player.user.get_full_name().strip()
    return full_name or player.user.username


def referee_team_players(team: TournamentTeam) -> list[Player]:
    """Return the linked club-team roster active on the tournament date."""
    if team.linked_team_id is None:
        return []
    event_date = team.tournament.starts_at.astimezone(
        ZoneInfo(team.tournament.timezone)
    ).date()
    return list(
        Player.objects
        .select_related("user")
        .filter(
            team_data_as_player__team_id=team.linked_team_id,
            team_data_as_player__season__start_date__lte=event_date,
            team_data_as_player__season__end_date__gte=event_date,
        )
        .distinct()
        .order_by("user__first_name", "user__last_name", "user__username")
    )


def _referee_match_payload(match: TournamentMatch) -> dict[str, Any]:
    closed_statuses = {
        TournamentMatch.Status.FINAL,
        TournamentMatch.Status.CANCELLED,
    }
    return {
        "id_uuid": str(match.id_uuid),
        "match_number": match.match_number,
        "status": match.status,
        "starts_at": match.starts_at.isoformat() if match.starts_at else None,
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
        "claimed_by": match.referee_name or None,
        "can_claim": (
            match.status not in closed_statuses and match.referee_claimed_at is None
        ),
    }


def build_referee_duties_state(team: TournamentTeam) -> dict[str, Any]:
    """Build the guest-facing list of matches assigned to one referee team."""
    matches = team.referee_matches.select_related(
        "field", "home_team", "away_team"
    ).order_by("starts_at", "match_number")
    players = referee_team_players(team)
    return {
        "access_kind": "team",
        "tournament": {
            "id_uuid": str(team.tournament_id),
            "name": team.tournament.name,
            "slug": team.tournament.slug,
            "timezone": team.tournament.timezone,
        },
        "team": {"id_uuid": str(team.id_uuid), "name": team.name},
        "players": [
            {"id_uuid": str(player.id_uuid), "name": _player_label(player)}
            for player in players
        ],
        "matches": [_referee_match_payload(match) for match in matches],
    }


def build_direct_referee_duty_state(match: TournamentMatch) -> dict[str, Any]:
    """Build a guest-facing claim screen for exactly one match QR."""
    team = match.referee_team
    players = referee_team_players(team) if team else []
    return {
        "access_kind": "match",
        "tournament": {
            "id_uuid": str(match.tournament_id),
            "name": match.tournament.name,
            "slug": match.tournament.slug,
            "timezone": match.tournament.timezone,
        },
        "team": ({"id_uuid": str(team.id_uuid), "name": team.name} if team else None),
        "players": [
            {"id_uuid": str(player.id_uuid), "name": _player_label(player)}
            for player in players
        ],
        "matches": [_referee_match_payload(match)],
    }


@transaction.atomic
def ensure_referee_access_token(team: TournamentTeam) -> UUID:
    """Create a stable, unguessable team-duty token when its QR is first opened."""
    locked_team = TournamentTeam.objects.select_for_update().get(pk=team.pk)
    if locked_team.referee_access_token is None:
        locked_team.referee_access_token = uuidv7()
        locked_team.save(update_fields=["referee_access_token"])
    return locked_team.referee_access_token


@transaction.atomic
def ensure_match_referee_access_token(match: TournamentMatch) -> UUID:
    """Create the stable credential embedded in one match's printable QR."""
    locked_match = TournamentMatch.objects.select_for_update(of=("self",)).get(
        pk=match.pk
    )
    if locked_match.referee_access_token is None:
        locked_match.referee_access_token = uuidv7()
        locked_match.save(update_fields=["referee_access_token"])
    return locked_match.referee_access_token


def assign_referee_team(
    match: TournamentMatch,
    *,
    team_id: UUID | None,
    reset_claim: bool = False,
) -> None:
    """Assign one tournament team to a match and clear obsolete claims.

    Raises:
        RefereeTrackerError: If the match is closed or the team is invalid.

    """
    if match.status in {
        TournamentMatch.Status.FINAL,
        TournamentMatch.Status.CANCELLED,
    }:
        raise RefereeTrackerError(
            "Een afgeronde wedstrijd kan geen nieuwe scheidsrechter krijgen."
        )
    team = None
    if team_id is not None:
        try:
            team = match.tournament.teams.get(pk=team_id, withdrawn=False)
        except TournamentTeam.DoesNotExist as exc:
            raise RefereeTrackerError("Kies een actief team uit dit toernooi.") from exc
        if team.pk in {match.home_team_id, match.away_team_id}:
            raise RefereeTrackerError(
                "Een spelend team kan niet zijn eigen wedstrijd fluiten."
            )
        _ensure_referee_team_available(match, team)

    assignment_changed = match.referee_team_id != getattr(team, "pk", None)
    if not assignment_changed and not reset_claim:
        return
    match.referee_team = team
    match.referee_name = ""
    match.referee_player = None
    match.referee_claim_token = None
    match.referee_claimed_at = None
    match.revision += 1
    match.save(
        update_fields=[
            "referee_team",
            "referee_name",
            "referee_player",
            "referee_claim_token",
            "referee_claimed_at",
            "revision",
            "updated_at",
        ]
    )


def _ensure_referee_team_available(
    match: TournamentMatch,
    team: TournamentTeam,
) -> None:
    """Reject a duty that overlaps another playing or referee assignment.

    Raises:
        RefereeTrackerError: If the team is already occupied at this time.

    """
    if match.starts_at is None:
        return
    match_end = match.starts_at + timedelta(minutes=match.duration_minutes)
    other_matches = (
        match.tournament.matches
        .filter(starts_at__isnull=False)
        .exclude(pk=match.pk)
        .exclude(status=TournamentMatch.Status.CANCELLED)
        .filter(Q(home_team=team) | Q(away_team=team) | Q(referee_team=team))
        .order_by("starts_at", "match_number")
    )
    for other in other_matches:
        if other.starts_at is None:
            continue
        other_end = other.starts_at + timedelta(minutes=other.duration_minutes)
        if match.starts_at < other_end and other.starts_at < match_end:
            raise RefereeTrackerError(
                f"{team.name} speelt of fluit wedstrijd {other.match_number} "
                "al op dit tijdstip."
            )


def claim_referee_duty(
    match: TournamentMatch,
    *,
    team: TournamentTeam,
    name: str,
    player_id: UUID | None,
) -> UUID:
    """Claim one unfinished match using its assigned team's QR credential.

    Raises:
        RefereeTrackerError: If the duty is unavailable or identity is invalid.

    """
    if match.referee_team_id != team.pk:
        raise RefereeTrackerError("Deze wedstrijd is niet aan jouw team toegewezen.")
    return _claim_referee_identity(
        match,
        team=team,
        name=name,
        player_id=player_id,
    )


def claim_direct_referee_duty(
    match: TournamentMatch,
    *,
    name: str,
    player_id: UUID | None,
) -> UUID:
    """Claim exactly the unfinished match encoded by a printable match QR."""
    return _claim_referee_identity(
        match,
        team=match.referee_team,
        name=name,
        player_id=player_id,
    )


def _claim_referee_identity(
    match: TournamentMatch,
    *,
    team: TournamentTeam | None,
    name: str,
    player_id: UUID | None,
) -> UUID:
    if match.status in {
        TournamentMatch.Status.FINAL,
        TournamentMatch.Status.CANCELLED,
    }:
        raise RefereeTrackerError("Deze wedstrijd is al afgerond.")
    if match.referee_claimed_at is not None:
        raise RefereeTrackerError(
            f"Deze wedstrijd is al geclaimd door {match.referee_name}."
        )

    player = None
    referee_name = name.strip()
    if player_id is not None:
        if team is None:
            raise RefereeTrackerError("Vul je naam in om deze wedstrijd te openen.")
        players = {player.id_uuid: player for player in referee_team_players(team)}
        player = players.get(player_id)
        if player is None:
            raise RefereeTrackerError("Kies een speler uit het toegewezen team.")
        referee_name = _player_label(player)
    if not referee_name:
        raise RefereeTrackerError("Vul je naam in of kies jezelf uit de spelerslijst.")

    token = cast(UUID, uuidv7())
    match.referee_name = referee_name
    match.referee_player = player
    match.referee_claim_token = token
    match.referee_claimed_at = timezone.now()
    match.revision += 1
    match.save(
        update_fields=[
            "referee_name",
            "referee_player",
            "referee_claim_token",
            "referee_claimed_at",
            "revision",
            "updated_at",
        ]
    )
    return token


def valid_guest_referee_claim(match: TournamentMatch, token: str | None) -> bool:
    """Return whether a claim token still authorizes this unfinished match."""
    if (
        not token
        or match.referee_claim_token is None
        or match.referee_claimed_at is None
        or match.status
        in {TournamentMatch.Status.FINAL, TournamentMatch.Status.CANCELLED}
    ):
        return False
    return compare_digest(token, str(match.referee_claim_token))


def _audit_matches_current_score(
    match: TournamentMatch, audit: TournamentResultAudit
) -> bool:
    return (
        audit.new_home_score == match.home_score
        and audit.new_away_score == match.away_score
        and audit.new_status == match.status
    )


def latest_referee_goal(match: TournamentMatch) -> TournamentResultAudit | None:
    """Return the newest goal that currently determines the visible score."""
    latest_change = match.result_audits.order_by("-created_at", "-id_uuid").first()
    if latest_change is None or latest_change.source not in {
        TournamentResultAudit.Source.REFEREE_GOAL,
        TournamentResultAudit.Source.REFEREE_UNDO,
    }:
        return None
    if (
        latest_change.source == TournamentResultAudit.Source.REFEREE_GOAL
        and _audit_matches_current_score(match, latest_change)
    ):
        return latest_change
    if latest_change.source == TournamentResultAudit.Source.REFEREE_UNDO:
        return (
            match.result_audits
            .filter(
                source=TournamentResultAudit.Source.REFEREE_GOAL,
                new_home_score=match.home_score,
                new_away_score=match.away_score,
                new_status=match.status,
            )
            .order_by("-created_at", "-id_uuid")
            .first()
        )
    return None


def _latest_event_payload(match: TournamentMatch) -> dict[str, Any] | None:
    audit = latest_referee_goal(match)
    if audit is None:
        return None
    previous_home = audit.previous_home_score or 0
    new_home = audit.new_home_score or 0
    side = "home" if new_home == previous_home + 1 else "away"
    team = match.home_team if side == "home" else match.away_team
    return {
        "id_uuid": str(audit.id_uuid),
        "type": "goal",
        "side": side,
        "team_name": team.name if team else "Onbekend team",
        "home_score": audit.new_home_score,
        "away_score": audit.new_away_score,
        "created_at": audit.created_at.isoformat(),
    }


def build_referee_tracker_state(match: TournamentMatch) -> dict[str, Any]:
    """Return only the match data needed by the field referee."""
    return {
        "tournament": {
            "id_uuid": str(match.tournament_id),
            "name": match.tournament.name,
            "slug": match.tournament.slug,
            "timezone": match.tournament.timezone,
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
        "latest_event": _latest_event_payload(match),
    }


def mark_field_ready(
    match: TournamentMatch,
    *,
    actor: object | None,
    actor_name: str = "",
) -> None:
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
    match.field_ready_by_name = actor_name or str(actor or "")
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


def record_goal(
    match: TournamentMatch,
    *,
    side: str,
    actor: object | None,
    actor_name: str = "",
) -> None:
    """Increment one team score and keep the direct-result audit complete.

    Raises:
        RefereeTrackerError: If the fixture cannot accept the goal.

    """
    if match.status != TournamentMatch.Status.LIVE:
        if match.status == TournamentMatch.Status.SCHEDULED:
            raise RefereeTrackerError(
                "Wacht tot de toernooileiding de ronde heeft gestart."
            )
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

    TournamentResultAudit.objects.create(
        match=match,
        previous_home_score=previous_home_score,
        previous_away_score=previous_away_score,
        new_home_score=home_score,
        new_away_score=away_score,
        previous_status=previous_status,
        new_status=match.status,
        reason="Goal recorded by referee tracker",
        changed_by=actor,
        changed_by_name=actor_name or str(actor or ""),
        source=TournamentResultAudit.Source.REFEREE_GOAL,
    )
    match.home_score = home_score
    match.away_score = away_score
    match.winner = None
    match.revision += 1
    match.save(
        update_fields=[
            "home_score",
            "away_score",
            "winner",
            "revision",
            "updated_at",
        ]
    )


def remove_latest_goal(
    match: TournamentMatch,
    *,
    event_id: UUID,
    actor: object | None,
    actor_name: str = "",
) -> None:
    """Restore the score before the exact latest referee goal.

    Raises:
        RefereeTrackerError: If the event is stale, closed, or no longer reversible.

    """
    if match.status in {
        TournamentMatch.Status.FINAL,
        TournamentMatch.Status.CANCELLED,
    }:
        raise RefereeTrackerError("Een afgeronde wedstrijd kan niet worden gewijzigd.")
    audit = latest_referee_goal(match)
    if audit is None or audit.id_uuid != event_id:
        raise RefereeTrackerError(
            "Dit is niet meer de laatste invoer. De nieuwste stand wordt getoond."
        )
    if not _audit_matches_current_score(match, audit):
        raise RefereeTrackerError(
            "De stand is inmiddels gewijzigd. De nieuwste stand wordt getoond."
        )

    TournamentResultAudit.objects.create(
        match=match,
        previous_home_score=match.home_score,
        previous_away_score=match.away_score,
        new_home_score=audit.previous_home_score,
        new_away_score=audit.previous_away_score,
        previous_status=match.status,
        new_status=audit.previous_status,
        reason="Latest referee goal removed",
        changed_by=actor,
        changed_by_name=actor_name or str(actor or ""),
        source=TournamentResultAudit.Source.REFEREE_UNDO,
    )
    match.home_score = audit.previous_home_score
    match.away_score = audit.previous_away_score
    match.status = audit.previous_status
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
