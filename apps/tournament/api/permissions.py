"""Role checks for tournament management and scoring."""

from __future__ import annotations

from apps.tournament.models import Tournament, TournamentMatch, TournamentMember


def is_authenticated(user: object) -> bool:
    """Return whether the object represents an authenticated request user."""
    return bool(user and getattr(user, "is_authenticated", False))


def can_manage_tournament(user: object, tournament: Tournament) -> bool:
    """Return whether a user can change structure, rules, and membership."""
    if not is_authenticated(user):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    if tournament.owner_id == getattr(user, "pk", None):
        return True
    return TournamentMember.objects.filter(
        tournament=tournament,
        user=user,
        role=TournamentMember.Role.MANAGER,
    ).exists()


def can_score_match(user: object, match: TournamentMatch) -> bool:
    """Return whether a user may enter this match's result."""
    if can_manage_tournament(user, match.tournament):
        return True
    if not is_authenticated(user):
        return False
    roles = TournamentMember.objects.filter(
        tournament=match.tournament,
        user=user,
        role=TournamentMember.Role.SCOREKEEPER,
    )
    return (
        roles.filter(field__isnull=True).exists()
        or roles.filter(field_id=match.field_id).exists()
    )
