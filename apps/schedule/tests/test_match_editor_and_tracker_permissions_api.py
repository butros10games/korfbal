"""Regression tests for schedule permission boundaries.

These tests lock down the intended access control for match event editor and
match tracker endpoints.
"""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.test import override_settings
from django.test.client import Client, RequestFactory
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GoalType, MatchData, MatchPart, Shot
from apps.schedule.api.permissions import IsCoachOrAdmin
from apps.schedule.models import Match, Season
from apps.team.models import Team


TEST_PASSWORD = "pass1234"  # noqa: S105  # nosec B105 - test credential constant


def _create_match(*, start_time: timezone.datetime | None = None) -> Match:
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)
    home_team = Team.objects.create(name="Home", club=Club.objects.create(name="HC"))
    away_team = Team.objects.create(name="Away", club=Club.objects.create(name="AC"))
    return Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=start_time or timezone.now(),
    )


def _ensure_match_part(match: Match) -> MatchPart:
    match_data = MatchData.objects.get(match_link=match)
    return MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=timezone.now() - timezone.timedelta(minutes=1),
        end_time=None,
        active=True,
    )


@pytest.mark.django_db
def test_is_coach_or_admin_permission_rules() -> None:
    """IsCoachOrAdmin should accept staff and coach-group users."""
    rf = RequestFactory()
    perm = IsCoachOrAdmin()

    request = rf.get("/")
    request.user = AnonymousUser()
    assert perm.has_permission(request, object()) is False

    user = get_user_model().objects.create_user(
        username="plain",
        password=TEST_PASSWORD,
    )
    request.user = user
    assert perm.has_permission(request, object()) is False

    staff_user = get_user_model().objects.create_user(
        username="staff",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    request.user = staff_user
    assert perm.has_permission(request, object()) is True

    coach_user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    coach_group, _created = Group.objects.get_or_create(name="Coach")
    coach_user.groups.add(coach_group)
    request.user = coach_user
    assert perm.has_permission(request, object()) is True


@pytest.mark.django_db
def test_is_coach_or_admin_handles_group_errors() -> None:
    """IsCoachOrAdmin should deny access if the user's groups cannot be read."""

    class _ExplodingGroupsUser:
        is_authenticated = True
        is_staff = False
        is_superuser = False

        @property
        def groups(self) -> object:
            raise RuntimeError("boom")

    rf = RequestFactory()
    request = rf.get("/")
    request.user = _ExplodingGroupsUser()

    perm = IsCoachOrAdmin()
    assert perm.has_permission(request, object()) is False


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_events_can_edit_reflects_permission(client: Client) -> None:
    """The can-edit endpoint should mirror IsCoachOrAdmin behavior."""
    match = _create_match()

    response = client.get(f"/api/matches/{match.id_uuid}/events/can-edit/")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"can_edit": False}

    coach_user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    coach_group, _created = Group.objects.get_or_create(name="Coach")
    coach_user.groups.add(coach_group)
    client.force_login(coach_user)

    response = client.get(f"/api/matches/{match.id_uuid}/events/can-edit/")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"can_edit": True}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_goal_editor_requires_coach_or_admin(client: Client) -> None:
    """Goal editor endpoints should be forbidden for normal authenticated users."""
    match = _create_match()
    match_part = _ensure_match_part(match)
    goal_type = GoalType.objects.create(name="Doorloop")

    normal_user = get_user_model().objects.create_user(
        username="viewer",
        password=TEST_PASSWORD,
    )
    client.force_login(normal_user)

    response = client.post(
        f"/api/matches/{match.id_uuid}/events/goals/",
        data={
            "player_id": str(normal_user.player.id_uuid),
            "team_id": str(match.home_team.id_uuid),
            "shot_type_id": str(goal_type.id_uuid),
            "match_part_id": str(match_part.id_uuid),
            "minute": 0,
        },
        content_type="application/json",
    )
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_goal_editor_create_update_delete_flow(client: Client) -> None:
    """Coaches should be able to create/update/delete goal events."""
    match = _create_match()
    match_part = _ensure_match_part(match)
    goal_type = GoalType.objects.create(name="Doorloop")

    coach_user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    coach_group, _created = Group.objects.get_or_create(name="Coach")
    coach_user.groups.add(coach_group)
    client.force_login(coach_user)

    create_response = client.post(
        f"/api/matches/{match.id_uuid}/events/goals/",
        data={
            "player_id": str(coach_user.player.id_uuid),
            "team_id": str(match.home_team.id_uuid),
            "shot_type_id": str(goal_type.id_uuid),
            "match_part_id": str(match_part.id_uuid),
            "minute": 0,
        },
        content_type="application/json",
    )
    assert create_response.status_code == HTTPStatus.CREATED

    created = create_response.json()
    assert created["type"] == "goal"
    assert created["team_id"] == str(match.home_team.id_uuid)
    assert created["player"] == "coach"

    shot_id = created["event_id"]
    shot_model = Shot.objects.get(id_uuid=shot_id)
    assert shot_model.for_team is True

    update_response = client.patch(
        f"/api/matches/{match.id_uuid}/events/goals/{shot_id}/",
        data={"for_team": False},
        content_type="application/json",
    )
    assert update_response.status_code == HTTPStatus.OK
    updated = update_response.json()
    assert updated["for_team"] is False

    shot_model.refresh_from_db()
    assert shot_model.for_team is False

    delete_response = client.delete(
        f"/api/matches/{match.id_uuid}/events/goals/{shot_id}/",
    )
    assert delete_response.status_code == HTTPStatus.NO_CONTENT
    assert Shot.objects.filter(id_uuid=shot_id).exists() is False


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_tracker_endpoints_are_coach_only(client: Client) -> None:
    """Tracker state/commands/poll endpoints should require coach/admin."""
    match = _create_match()

    normal_user = get_user_model().objects.create_user(
        username="viewer",
        password=TEST_PASSWORD,
    )
    client.force_login(normal_user)

    state_url = f"/api/matches/{match.id_uuid}/tracker/{match.home_team.id_uuid}/state/"
    response = client.get(state_url)
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}

    coach_user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    coach_group, _created = Group.objects.get_or_create(name="Coach")
    coach_user.groups.add(coach_group)
    client.force_login(coach_user)

    with patch(
        "apps.schedule.api.views.get_tracker_state",
        return_value={"score": {"for": 1, "against": 2}},
    ) as mocked_state:
        response = client.get(state_url)
        assert response.status_code == HTTPStatus.OK
        payload = response.json()
        assert payload["score"] == {"for": 1, "against": 2}
        assert mocked_state.call_count == 1


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_tracker_command_rejects_non_object_json(client: Client) -> None:
    """Tracker command should reject JSON arrays (expects an object/dict)."""
    match = _create_match()

    coach_user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    coach_group, _created = Group.objects.get_or_create(name="Coach")
    coach_user.groups.add(coach_group)
    client.force_login(coach_user)

    command_url = (
        f"/api/matches/{match.id_uuid}/tracker/{match.home_team.id_uuid}/commands/"
    )

    with patch("apps.schedule.api.views.apply_tracker_command") as mocked_apply:
        response = client.post(
            command_url,
            data="[]",
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.json() == {"detail": "Invalid JSON body."}
        assert mocked_apply.call_count == 0


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_tracker_poll_rejects_invalid_since_timestamp(client: Client) -> None:
    """Tracker poll should return 400 for invalid `since` params."""
    match = _create_match()

    coach_user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    coach_group, _created = Group.objects.get_or_create(name="Coach")
    coach_user.groups.add(coach_group)
    client.force_login(coach_user)

    poll_url = f"/api/matches/{match.id_uuid}/tracker/{match.home_team.id_uuid}/poll/"
    response = client.get(poll_url, {"since": "not-a-timestamp"})
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"detail": "Invalid 'since' timestamp."}
