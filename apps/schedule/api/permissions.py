"""Custom API permissions for korfbal schedule endpoints."""

from __future__ import annotations

from datetime import date
from typing import Any, cast

from django.db.models import Q
from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from apps.player.models.player import Player
from apps.player.models.player_club_membership import PlayerClubMembership
from apps.schedule.models import Match
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData


class IsCoachOrAdmin(BasePermission):
    """Allow access to authenticated coaches and admins.

    Rules:
    - Admins: Django staff users.
    - Coaches: users in the "coach" group (case-insensitive).

    Note: this is intentionally simple. If you later model coach/team roles,
    replace the group check with that domain logic.
    """

    message = "You do not have permission to edit match events."

    def has_permission(self, request: Request, view: object) -> bool:
        """Return True if the request user is an admin or coach."""
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return True

        # Group name-based permission for coaches.
        try:
            groups = getattr(user, "groups", None)
            if groups is None:
                return False
            return bool(cast(Any, groups).filter(name__iexact="coach").exists())
        except Exception:
            return False


class IsClubMemberOrCoachOrAdmin(BasePermission):
    """Allow access to club members, coaches, and admins for match tracking."""

    message = "You do not have permission to access the match tracker."

    def has_permission(self, request: Request, view: object) -> bool:
        """Return True if the request user is a club member, coach, or admin."""
        user = request.user
        if not user or not user.is_authenticated:
            return False

        if self._is_admin(user) or self._is_coach(user):
            return True

        player = getattr(user, "player", None)
        if not isinstance(player, Player):
            return False

        match, team = self._get_match_and_team(view)
        if not match or not team:
            return False

        return self._has_club_access(player, match, team)

    def _is_admin(self, user: object) -> bool:
        return bool(
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        )

    def _is_coach(self, user: object) -> bool:
        try:
            groups = getattr(user, "groups", None)
            return bool(
                groups and cast(Any, groups).filter(name__iexact="coach").exists()
            )
        except Exception:
            return False

    def _get_match_and_team(self, view: object) -> tuple[Match | None, Team | None]:
        view_kwargs = getattr(view, "kwargs", {})
        match_id = view_kwargs.get("pk") or view_kwargs.get("id_uuid")
        team_id = view_kwargs.get("team_id")
        if not match_id or not team_id:
            return None, None

        match = (
            Match.objects
            .select_related("home_team__club", "away_team__club")
            .filter(id_uuid=match_id)
            .first()
        )
        team = Team.objects.select_related("club").filter(id_uuid=team_id).first()
        if not match or not team:
            return None, None

        if team.id_uuid not in {match.home_team_id, match.away_team_id}:
            return None, None

        return match, team

    def _has_club_access(self, player: Player, match: Match, team: Team) -> bool:
        match_date: date = match.start_time.date()
        membership_allowed = (
            PlayerClubMembership.objects
            .filter(
                player=player,
                club=team.club,
                start_date__lte=match_date,
            )
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=match_date))
            .exists()
        )
        if membership_allowed:
            return True

        return (
            TeamData.objects
            .filter(
                team__club=team.club,
                season=match.season,
            )
            .filter(Q(players=player) | Q(coach=player))
            .exists()
        )
