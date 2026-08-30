"""Focused permission contracts for match editing and tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any, cast

from django.contrib.auth.models import AnonymousUser
from django.test.client import Client, RequestFactory
from django.utils import timezone
import pytest
from rest_framework.permissions import BasePermission

from apps.player.models.player_club_membership import PlayerClubMembership
from apps.schedule.api.permissions import IsClubMemberOrCoachOrAdmin, IsCoachOrAdmin
from apps.schedule.tests import match_api_test_support as support
from apps.team.models import Team
from apps.team.models.team_data import TeamData


pytestmark = pytest.mark.django_db


class _UserWithoutPlayer:
    is_authenticated = True
    is_staff = False
    is_superuser = False


def _allows(
    permission: BasePermission,
    user: object,
    graph: support.MatchGraph | None = None,
    team: Team | None = None,
) -> bool:
    request = RequestFactory().get("/")
    request.user = cast(Any, user)
    kwargs: dict[str, str] = {}
    if graph:
        kwargs["id"] = str(graph.match.id_uuid)
    if team:
        kwargs["team_id"] = str(team.id_uuid)
    view = SimpleNamespace(lookup_url_kwarg="id", kwargs=kwargs)
    return permission.has_permission(cast(Any, request), cast(Any, view))


def _tracker_allows(user: object, graph: support.MatchGraph, team: Team) -> bool:
    return _allows(IsClubMemberOrCoachOrAdmin(), user, graph, team=team)


@pytest.mark.parametrize("identity", ["anonymous", "plain", "playerless"])
def test_editor_permission_denies_non_editor_identities(identity: str) -> None:
    """Deny anonymous, ordinary, and playerless users."""
    graph = support.create_match_graph(prefix=f"denied-{identity}")
    if identity == "anonymous":
        user: object = AnonymousUser()
    elif identity == "playerless":
        user = _UserWithoutPlayer()
    else:
        user = support.create_user(username="plain-editor-user")
    assert _allows(IsCoachOrAdmin(), user, graph) is False


def test_editor_permission_allows_staff_without_resolving_a_match() -> None:
    """Allow staff without match route resolution."""
    staff = support.create_user(username="staff-editor")
    cast(Any, staff).is_staff = True
    staff.save(update_fields=["is_staff"])
    assert _allows(IsCoachOrAdmin(), staff) is True


def test_editor_permission_allows_assigned_participating_team_coach() -> None:
    """Allow either participating team's assigned coach."""
    graph = support.create_match_graph(prefix="assigned-coach")
    coach = support.create_user(username="assigned-coach")
    support.assign_coach(graph, coach, team=graph.away_team)
    assert _allows(IsCoachOrAdmin(), coach, graph) is True


def test_editor_permission_scopes_team_specific_access() -> None:
    """Limit a team-specific route to that team's coach."""
    graph = support.create_match_graph(prefix="team-scoped-coach")
    coach = support.create_user(username="home-only-coach")
    support.assign_coach(graph, coach, team=graph.home_team)
    assert _allows(IsCoachOrAdmin(), coach, graph, team=graph.home_team) is True
    assert _allows(IsCoachOrAdmin(), coach, graph, team=graph.away_team) is False


@pytest.mark.parametrize("identity", ["anonymous", "outsider"])
def test_tracker_permission_denies_users_without_club_access(identity: str) -> None:
    """Require authentication and a club relationship."""
    graph = support.create_match_graph(prefix=f"tracker-{identity}")
    user = (
        AnonymousUser()
        if identity == "anonymous"
        else support.create_user(username="tracker-outsider")
    )
    assert _tracker_allows(user, graph, graph.home_team) is False


def test_tracker_permission_uses_match_local_membership_date() -> None:
    """Evaluate membership on the local match date."""
    graph = support.create_match_graph(
        prefix="local-membership",
        start_time=datetime(2026, 7, 4, 22, 30, tzinfo=UTC),
    )
    member = support.create_user(username="local-date-member")
    PlayerClubMembership.objects.create(
        player=cast(Any, member).player,
        club=graph.home_team.club,
        start_date=timezone.localdate(graph.match.start_time),
    )
    assert _tracker_allows(member, graph, graph.home_team) is True
    assert _tracker_allows(member, graph, graph.away_team) is False


@pytest.mark.parametrize("relationship", ["coach", "players"])
def test_tracker_permission_allows_season_team_relationship(relationship: str) -> None:
    """Allow a season coach or roster player."""
    graph = support.create_match_graph(prefix=f"tracker-{relationship}")
    user = support.create_user(username=f"tracker-{relationship}")
    team_data = TeamData.objects.create(
        team=graph.home_team,
        season=graph.match.season,
    )
    getattr(team_data, relationship).add(cast(Any, user).player)
    assert _tracker_allows(user, graph, graph.home_team) is True


def test_can_edit_endpoint_reflects_anonymous_and_coach_access(client: Client) -> None:
    """Report anonymous and coach editing capability."""
    graph = support.create_match_graph(prefix="can-edit")
    url = f"/api/matches/{graph.match.id_uuid}/events/can-edit/"
    response = client.get(url)
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"can_edit": False}

    coach = support.create_user(username="can-edit-coach")
    support.assign_coach(graph, coach)
    client.force_login(coach)
    response = client.get(url)
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"can_edit": True}


def test_goal_editor_rejects_outsider_before_validation(client: Client) -> None:
    """Reject outsiders before payload validation."""
    graph = support.create_match_graph(prefix="goal-denied")
    client.force_login(support.create_user(username="goal-outsider"))
    response = client.post(
        f"/api/matches/{graph.match.id_uuid}/events/goals/",
        data={},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
