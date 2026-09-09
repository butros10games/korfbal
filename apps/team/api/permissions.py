"""Viewer access rules for team rosters and goal-song moderation."""

from __future__ import annotations

from rest_framework.request import Request

from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData


def viewer_player(request: Request) -> Player | None:
    """Return the authenticated viewer's player profile when it exists."""
    if not request.user.is_authenticated:
        return None
    return Player.objects.filter(user=request.user).first()


def viewer_can_manage_team(
    *,
    request: Request,
    team: Team,
    season: Season | None,
) -> bool:
    """Allow staff or roster managers to moderate team goal songs."""
    user = request.user
    if not user.is_authenticated:
        return False
    if bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        return True
    return viewer_can_manage_roster(
        request=request,
        team=team,
        season=season,
    )


def viewer_can_manage_roster(
    *,
    request: Request,
    team: Team,
    season: Season | None,
) -> bool:
    """Limit roster management to managers associated with the club."""
    if not request.user.is_authenticated:
        return False

    viewer = viewer_player(request)
    if viewer is None:
        return False

    if team.club.admin.filter(id_uuid=viewer.id_uuid).exists():
        return True

    team_data_qs = TeamData.objects.filter(team=team, coach=viewer)
    if season is not None:
        team_data_qs = team_data_qs.filter(season=season)
    return team_data_qs.exists()
