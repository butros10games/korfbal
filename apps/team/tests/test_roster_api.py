"""Season membership authorization and incremental mutation contracts."""

from datetime import timedelta
from uuid import uuid4

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.schedule.models import Season
from apps.team.models import TeamData
from apps.team.tests.team_test_support import (
    TeamTestContext,
    build_team_context,
    create_player,
)


pytestmark = pytest.mark.django_db


def roster_url(context: TeamTestContext, suffix: str = "roster") -> str:
    """Build a season-scoped route."""
    return (
        f"/api/team/teams/{context.team.id_uuid}/{suffix}/"
        f"?season={context.season.id_uuid}"
    )


@pytest.mark.parametrize("role", ["coach", "club_admin", "staff"])
def test_managers_can_add_and_remove_without_touching_other_seasons(role: str) -> None:
    """Managers can add and remove without touching other seasons."""
    context = build_team_context()
    manager = context.coach if role == "coach" else create_player(username=role)
    if role == "club_admin":
        context.club.admin.add(manager)
    if role == "staff":
        manager.user.is_staff = True
        manager.user.save(update_fields=["is_staff"])
    other_season = Season.objects.create(
        name="Other",
        start_date=context.season.start_date - timedelta(days=365),
        end_date=context.season.start_date - timedelta(days=1),
    )
    other = TeamData.objects.create(team=context.team, season=other_season)
    other.players.add(context.player)
    client = APIClient()
    client.force_authenticate(manager.user)
    candidate = create_player(username="new.player")
    payload = {"player": str(candidate.pk), "operation": "add"}
    for _ in range(2):
        response = client.patch(roster_url(context), payload, format="json")
        assert response.status_code == status.HTTP_200_OK
    assert set(context.team_data.players.values_list("pk", flat=True)) == {
        candidate.pk,
        context.player.pk,
    }
    response = client.patch(
        roster_url(context),
        {
            "player": str(context.player.pk),
            "operation": "remove",
        },
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    assert list(context.team_data.players.all()) == [candidate]
    assert list(other.players.all()) == [context.player]


@pytest.mark.parametrize("role", ["anonymous", "player", "other_coach"])
def test_non_managers_cannot_mutate_or_search(role: str) -> None:
    """Non managers cannot mutate or search."""
    context = build_team_context()
    client = APIClient()
    if role != "anonymous":
        viewer = (
            context.player
            if role == "player"
            else build_team_context(suffix="other").coach
        )
        client.force_authenticate(viewer.user)
    response = client.get(roster_url(context))
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["can_manage"] is False
    assert client.patch(
        roster_url(context),
        {
            "player": str(context.player.pk),
            "operation": "remove",
        },
        format="json",
    ).status_code in {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN}
    assert client.get(
        roster_url(context, "roster-candidates") + "&search=player"
    ).status_code in {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN}
    assert context.team_data.players.filter(pk=context.player.pk).exists()


@pytest.mark.parametrize(
    "season", ["", "invalid", "ffffffff-ffff-4fff-8fff-ffffffffffff"]
)
def test_invalid_season_never_falls_back(season: str) -> None:
    """Invalid season never falls back."""
    context = build_team_context()
    client = APIClient()
    client.force_authenticate(context.coach.user)
    response = client.patch(
        f"/api/team/teams/{context.team.pk}/roster/?season={season}",
        {"player": str(context.player.pk), "operation": "remove"},
        format="json",
    )
    assert response.status_code in {
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_404_NOT_FOUND,
    }
    assert context.team_data.players.filter(pk=context.player.pk).exists()


def test_search_is_server_filtered_and_excludes_members() -> None:
    """Search is server filtered excludes members and reports truncation."""
    context = build_team_context()
    client = APIClient()
    client.force_authenticate(context.coach.user)
    candidate = create_player(username="search.target")
    response = client.get(
        roster_url(context, "roster-candidates") + "&search=SEARCH.TAR"
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "players": [{"id_uuid": str(candidate.pk), "username": "search.target"}],
        "has_more": False,
    }
    response = client.get(
        roster_url(context, "roster-candidates") + "&search=player_contract"
    )
    assert response.json()["players"] == []


def test_coach_from_another_season_cannot_edit() -> None:
    """Coach from another season cannot edit."""
    context = build_team_context()
    season = Season.objects.create(
        name="Future",
        start_date=context.season.end_date + timedelta(days=1),
        end_date=context.season.end_date + timedelta(days=365),
    )
    client = APIClient()
    client.force_authenticate(context.coach.user)
    response = client.patch(
        f"/api/team/teams/{context.team.pk}/roster/?season={season.pk}",
        {"player": str(context.player.pk), "operation": "add"},
        format="json",
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert not TeamData.objects.filter(team=context.team, season=season).exists()


def test_invalid_mutations_and_duplicate_legacy_rows() -> None:
    """Invalid mutations and duplicate legacy rows."""
    context = build_team_context()
    duplicate = TeamData.objects.create(team=context.team, season=context.season)
    duplicate.players.add(context.player)
    client = APIClient()
    client.force_authenticate(context.coach.user)
    for payload in [
        {"player": "invalid", "operation": "add"},
        {"player": str(uuid4()), "operation": "add"},
        {"player": str(context.player.pk), "operation": "replace"},
    ]:
        assert (
            client.patch(roster_url(context), payload, format="json").status_code
            == status.HTTP_400_BAD_REQUEST
        )
    assert (
        client.patch(
            roster_url(context),
            {"player": str(context.player.pk), "operation": "remove"},
            format="json",
        ).status_code
        == status.HTTP_200_OK
    )
    assert not duplicate.players.exists()
    assert not context.team_data.players.exists()


def test_staff_can_initialize_roster_and_search_is_bounded() -> None:
    """An empty season roster can be created and searches ask for refinement."""
    context = build_team_context()
    context.team_data.delete()
    context.coach.user.is_staff = True
    context.coach.user.save(update_fields=["is_staff"])
    client = APIClient()
    client.force_authenticate(context.coach.user)
    response = client.patch(
        roster_url(context),
        {
            "player": str(context.player.pk),
            "operation": "add",
        },
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    assert (
        TeamData.objects
        .get(team=context.team, season=context.season)
        .players.filter(pk=context.player.pk)
        .exists()
    )
    for index in range(21):
        create_player(username=f"candidate{index:02d}")
    result = client.get(
        roster_url(context, "roster-candidates") + "&search=candidate"
    ).json()
    assert result["has_more"] is True
    assert [player["username"] for player in result["players"]] == [
        f"candidate{index:02d}" for index in range(20)
    ]


def test_session_mutation_requires_csrf() -> None:
    """Browser sessions cannot unlink players without a CSRF token."""
    context = build_team_context()
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(context.coach.user)
    response = client.patch(
        roster_url(context),
        {"player": str(context.player.pk), "operation": "remove"},
        format="json",
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert context.team_data.players.filter(pk=context.player.pk).exists()
