"""Custom API permissions for korfbal schedule endpoints."""

from __future__ import annotations

from datetime import date

from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from apps.player.models.player import Player
from apps.player.models.player_club_membership import PlayerClubMembership
from apps.schedule.models import Match
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData


class IsCoachOrAdmin(BasePermission):
    """Allow access to admins and coaches assigned to the requested match.

    Rules:
    - Admins: Django staff users.
    - Coaches: players assigned as coach to a participating team for the match
      season. Tracker commands with a team perspective require assignment to
      that specific team.
    """

    message = "You do not have permission to edit match events."

    def has_permission(self, request: Request, view: object) -> bool:
        """Return True if the request user is an admin or coach."""
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return True

        player = getattr(user, "player", None)
        if not isinstance(player, Player):
            return False

        match, team = _get_match_and_team(view, require_team=False)
        if match is None:
            return False

        team_ids = [team.id_uuid] if team else [match.home_team_id, match.away_team_id]
        return TeamData.objects.filter(
            season=match.season,
            team_id__in=team_ids,
            coach=player,
        ).exists()


class IsClubMemberOrCoachOrAdmin(BasePermission):
    """Allow access to club members, coaches, and admins for match tracking."""

    message = "You do not have permission to access the match tracker."

    def has_permission(self, request: Request, view: object) -> bool:
        """Return True if the request user is a club member, coach, or admin."""
        user = request.user
        if not user or not user.is_authenticated:
            return False

        match, team = _get_match_and_team(view, require_team=True)
        if not match or not team:
            return False

        if self._is_admin(user):
            return True

        player = getattr(user, "player", None)
        if not isinstance(player, Player):
            return False

        return self._has_club_access(player, match, team)

    def _is_admin(self, user: object) -> bool:
        return bool(
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        )

    def _has_club_access(self, player: Player, match: Match, team: Team) -> bool:
        match_date: date = timezone.localdate(match.start_time)
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


def _get_match_and_team(
    view: object,
    *,
    require_team: bool,
) -> tuple[Match | None, Team | None]:
    """Resolve and validate the match/team pair encoded in an action route."""
    view_kwargs = getattr(view, "kwargs", {})
    match_id = view_kwargs.get("pk") or view_kwargs.get("id_uuid")
    team_id = view_kwargs.get("team_id")
    if not match_id or (require_team and not team_id):
        return None, None

    match = (
        Match.objects
        .select_related("home_team__club", "away_team__club", "season")
        .filter(id_uuid=match_id)
        .first()
    )
    if match is None:
        return None, None
    if not team_id:
        return match, None

    team = Team.objects.select_related("club").filter(id_uuid=team_id).first()
    if team is None or team.id_uuid not in {match.home_team_id, match.away_team_id}:
        return None, None
    return match, team
