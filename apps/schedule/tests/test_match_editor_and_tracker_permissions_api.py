"""Regression tests for schedule permission boundaries.

These tests lock down the intended access control for match event editor and
match tracker endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import override_settings
from django.test.client import Client, RequestFactory
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchEvent,
    MatchLiveChange,
    MatchPart,
    MatchPlayer,
    Pause,
    Shot,
    ShotEventDetail,
    Timeout,
)
from apps.player.models import Player
from apps.player.models.player_club_membership import PlayerClubMembership
from apps.schedule.api.permissions import IsClubMemberOrCoachOrAdmin, IsCoachOrAdmin
from apps.schedule.models import Match, Season
from apps.team.models import Team
from apps.team.models.team_data import TeamData


TEST_PASSWORD = "pass1234"  # nosec B105 - test credential constant
CONFLICT_CLIENT_SEQUENCE = 4


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


def _assign_coach(match: Match, user: object, *, team: Team | None = None) -> None:
    player = getattr(user, "player", None)
    assert isinstance(player, Player)
    team_data, _created = TeamData.objects.get_or_create(
        team=team or match.home_team,
        season=match.season,
    )
    team_data.coach.add(player)


def _add_roster_player(match: Match, user: object, *, team: Team) -> Player:
    player = getattr(user, "player", None)
    assert isinstance(player, Player)
    MatchPlayer.objects.create(
        match_data=MatchData.objects.get(match_link=match),
        team=team,
        player=player,
    )
    return player


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
@pytest.mark.parametrize("event_kind", ["goals", "substitutes", "pauses", "timeouts"])
@pytest.mark.parametrize("method", ["patch", "delete"])
def test_missing_editor_event_does_not_publish_live_change(
    client: Client,
    event_kind: str,
    method: str,
) -> None:
    """A rejected detail mutation is a true no-op for live tracker state."""
    match = _create_match()
    coach_user = get_user_model().objects.create_user(
        username=f"missing-{event_kind}-{method}",
        password=TEST_PASSWORD,
    )
    _assign_coach(match, coach_user)
    client.force_login(coach_user)
    match_data = MatchData.objects.get(match_link=match)
    revision_before = match_data.live_revision
    changes_before = MatchLiveChange.objects.filter(match_data=match_data).count()
    url = f"/api/matches/{match.id_uuid}/events/{event_kind}/{uuid4()}/"

    response = getattr(client, method)(url, data={}, content_type="application/json")

    assert response.status_code == HTTPStatus.NOT_FOUND
    match_data.refresh_from_db()
    assert match_data.live_revision == revision_before
    assert (
        MatchLiveChange.objects.filter(match_data=match_data).count() == changes_before
    )


@pytest.mark.django_db
def test_is_coach_or_admin_permission_rules() -> None:
    """IsCoachOrAdmin should accept staff and assigned coaches."""
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


@pytest.mark.django_db
def test_is_club_member_or_coach_or_admin_permission_rules() -> None:
    """Club members should be allowed to access tracker endpoints."""
    rf = RequestFactory()
    perm = IsClubMemberOrCoachOrAdmin()
    match = _create_match()

    request = rf.get(
        f"/api/matches/{match.id_uuid}/tracker/{match.home_team.id_uuid}/state/"
    )
    request.user = AnonymousUser()
    view = SimpleNamespace(
        kwargs={"pk": str(match.id_uuid), "team_id": str(match.home_team.id_uuid)}
    )
    assert perm.has_permission(request, view) is False

    member_user = get_user_model().objects.create_user(
        username="member",
        password=TEST_PASSWORD,
    )
    PlayerClubMembership.objects.create(
        player=member_user.player,
        club=match.home_team.club,
        start_date=timezone.localdate(),
    )
    request.user = member_user
    assert perm.has_permission(request, view) is True

    non_member_user = get_user_model().objects.create_user(
        username="outsider",
        password=TEST_PASSWORD,
    )
    request.user = non_member_user
    assert perm.has_permission(request, view) is False

    coach_user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    request.user = coach_user
    assert perm.has_permission(request, view) is False
    _assign_coach(match, coach_user)
    assert perm.has_permission(request, view) is True


@pytest.mark.django_db
def test_is_club_member_permission_uses_match_local_date() -> None:
    """Membership checks should compare against the match date in local time."""
    rf = RequestFactory()
    perm = IsClubMemberOrCoachOrAdmin()
    match = _create_match(start_time=datetime(2026, 7, 4, 22, 30, tzinfo=UTC))
    member_user = get_user_model().objects.create_user(
        username="local-date-member",
        password=TEST_PASSWORD,
    )
    PlayerClubMembership.objects.create(
        player=member_user.player,
        club=match.home_team.club,
        start_date=timezone.localdate(match.start_time),
    )

    request = rf.get(
        f"/api/matches/{match.id_uuid}/tracker/{match.home_team.id_uuid}/state/"
    )
    request.user = member_user
    view = SimpleNamespace(
        kwargs={"pk": str(match.id_uuid), "team_id": str(match.home_team.id_uuid)}
    )

    assert perm.has_permission(request, view) is True


@pytest.mark.django_db
def test_is_coach_or_admin_handles_user_without_player() -> None:
    """IsCoachOrAdmin should deny access if the user has no player relation."""

    class _UserWithoutPlayer:
        is_authenticated = True
        is_staff = False
        is_superuser = False

    rf = RequestFactory()
    request = rf.get("/")
    request.user = _UserWithoutPlayer()

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
    _assign_coach(match, coach_user)
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
    _assign_coach(match, coach_user)
    _add_roster_player(match, coach_user, team=match.home_team)
    away_user = get_user_model().objects.create_user(
        username="away-scorer",
        password=TEST_PASSWORD,
    )
    away_player = _add_roster_player(match, away_user, team=match.away_team)
    client.force_login(coach_user)

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

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

    match_data.refresh_from_db()
    assert (match_data.home_score, match_data.away_score) == (1, 0)

    update_response = client.patch(
        f"/api/matches/{match.id_uuid}/events/goals/{shot_id}/",
        data={
            "for_team": True,
            "team_id": str(match.away_team.id_uuid),
            "player_id": str(away_player.id_uuid),
        },
        content_type="application/json",
    )
    assert update_response.status_code == HTTPStatus.OK
    updated = update_response.json()
    assert updated["for_team"] is False

    shot_model.refresh_from_db()
    assert shot_model.for_team is False
    canonical = ShotEventDetail.objects.get(
        event__match_data=match_data,
        event__source_id=shot_model.pk,
        event__status=MatchEvent.STATUS_ACTIVE,
    )
    assert canonical.shooter == away_player
    assert canonical.defender is None
    match_data.refresh_from_db()
    assert (match_data.home_score, match_data.away_score) == (0, 1)

    delete_response = client.delete(
        f"/api/matches/{match.id_uuid}/events/goals/{shot_id}/",
    )
    assert delete_response.status_code == HTTPStatus.NO_CONTENT
    assert Shot.objects.filter(id_uuid=shot_id).exists() is False
    match_data.refresh_from_db()
    assert (match_data.home_score, match_data.away_score) == (0, 0)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_editor_validates_roster_team_and_period_time(client: Client) -> None:
    """Editor goals cannot reference the wrong roster side or leave the period."""
    match = _create_match()
    match_part = _ensure_match_part(match)
    match_part.end_time = match_part.start_time + timezone.timedelta(minutes=10)
    match_part.active = False
    match_part.save(update_fields=["end_time", "active"])
    goal_type = GoalType.objects.create(name="Strict validation")
    coach_user = get_user_model().objects.create_user(
        username="strict-goal-coach",
        password=TEST_PASSWORD,
    )
    _assign_coach(match, coach_user)
    _add_roster_player(match, coach_user, team=match.home_team)
    client.force_login(coach_user)

    base_payload = {
        "player_id": str(coach_user.player.id_uuid),
        "team_id": str(match.away_team.id_uuid),
        "shot_type_id": str(goal_type.id_uuid),
        "match_part_id": str(match_part.id_uuid),
        "minute": 1,
    }
    wrong_team = client.post(
        f"/api/matches/{match.id_uuid}/events/goals/",
        data=base_payload,
        content_type="application/json",
    )
    assert wrong_team.status_code == HTTPStatus.BAD_REQUEST
    assert "player_id" in wrong_team.json()

    outside_period = client.post(
        f"/api/matches/{match.id_uuid}/events/goals/",
        data={**base_payload, "team_id": str(match.home_team.id_uuid), "minute": 11},
        content_type="application/json",
    )
    assert outside_period.status_code == HTTPStatus.BAD_REQUEST
    assert "time" in outside_period.json()


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_editor_minute_accounts_for_completed_pauses(client: Client) -> None:
    """A displayed match minute maps back through prior paused wall time."""
    match = _create_match()
    match_part = _ensure_match_part(match)
    Pause.objects.create(
        match_data=match_part.match_data,
        match_part=match_part,
        start_time=match_part.start_time + timezone.timedelta(minutes=5),
        end_time=match_part.start_time + timezone.timedelta(minutes=7),
        active=False,
    )
    goal_type = GoalType.objects.create(name="Pause-aware time")
    coach_user = get_user_model().objects.create_user(
        username="pause-aware-coach",
        password=TEST_PASSWORD,
    )
    _assign_coach(match, coach_user)
    _add_roster_player(match, coach_user, team=match.home_team)
    client.force_login(coach_user)

    response = client.post(
        f"/api/matches/{match.id_uuid}/events/goals/",
        data={
            "player_id": str(coach_user.player.id_uuid),
            "team_id": str(match.home_team.id_uuid),
            "shot_type_id": str(goal_type.id_uuid),
            "match_part_id": str(match_part.id_uuid),
            "minute": 8,
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CREATED
    shot = Shot.objects.get(pk=response.json()["event_id"])
    assert shot.time == match_part.start_time + timezone.timedelta(minutes=10)


@pytest.mark.django_db(transaction=True)
@override_settings(SECURE_SSL_REDIRECT=False)
def test_goal_editor_rolls_back_typed_write_when_event_envelope_fails(
    client: Client,
) -> None:
    """The editor commits a typed event and its audit envelope as one unit."""
    match = _create_match()
    match_part = _ensure_match_part(match)
    goal_type = GoalType.objects.create(name="Atomic goal")
    coach_user = get_user_model().objects.create_user(
        username="atomic-goal-coach",
        password=TEST_PASSWORD,
    )
    _assign_coach(match, coach_user)
    _add_roster_player(match, coach_user, team=match.home_team)
    client.force_login(coach_user)
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    with (
        patch(
            "apps.game_tracker.services.match_events.MatchEvent.objects.create",
            side_effect=RuntimeError("forced envelope failure"),
        ),
        pytest.raises(RuntimeError, match="forced envelope failure"),
    ):
        client.post(
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

    assert Shot.objects.filter(match_data=match_data).exists() is False


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_timeout_editor_uses_stable_pause_event_identity(client: Client) -> None:
    """Timeout deltas and editor routes should share one durable event id."""
    match = _create_match()
    match_part = _ensure_match_part(match)
    coach_user = get_user_model().objects.create_user(
        username="timeout-coach",
        password=TEST_PASSWORD,
    )
    _assign_coach(match, coach_user)
    client.force_login(coach_user)

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])
    match_data.refresh_from_db()
    revision_before_create = match_data.live_revision

    response = client.post(
        f"/api/matches/{match.id_uuid}/events/timeouts/",
        data={
            "team_id": str(match.home_team_id),
            "match_part_id": str(match_part.id_uuid),
            "minute": 0,
            "length_seconds": 20,
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CREATED
    created = response.json()
    assert created["event_kind"] == "timeout"
    assert created["event_id"] == created["pause_id"]
    assert created["timeout_id"] != created["pause_id"]

    delta = client.get(
        f"/api/matches/{match.id_uuid}/events/",
        {"since_revision": revision_before_create},
    ).json()
    assert [event["event_id"] for event in delta["upsert"]] == [created["event_id"]]

    update_response = client.patch(
        f"/api/matches/{match.id_uuid}/events/timeouts/{created['event_id']}/",
        data={"length_seconds": 30},
        content_type="application/json",
    )
    assert update_response.status_code == HTTPStatus.OK
    assert update_response.json()["event_id"] == created["event_id"]
    timeout = Timeout.objects.get(id_uuid=created["timeout_id"])
    assert str(timeout.pause_id) == created["event_id"]
    assert Pause.objects.get(
        id_uuid=created["pause_id"]
    ).length() == timezone.timedelta(seconds=30)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_tracker_endpoints_allow_club_members(client: Client) -> None:
    """Club members can view and operate their club's match tracker."""
    match = _create_match()
    command_url = (
        f"/api/matches/{match.id_uuid}/tracker/{match.home_team.id_uuid}/commands/"
    )

    normal_user = get_user_model().objects.create_user(
        username="viewer",
        password=TEST_PASSWORD,
    )
    client.force_login(normal_user)

    state_url = f"/api/matches/{match.id_uuid}/tracker/{match.home_team.id_uuid}/state/"
    response = client.get(state_url)
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}
    response = client.post(
        command_url,
        data={"command": "start/pause"},
        content_type="application/json",
    )
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}

    member_user = get_user_model().objects.create_user(
        username="member",
        password=TEST_PASSWORD,
    )
    PlayerClubMembership.objects.create(
        player=member_user.player,
        club=match.home_team.club,
        start_date=timezone.localdate(),
    )
    client.force_login(member_user)

    with patch(
        "apps.schedule.api.views.get_tracker_state",
        return_value={"score": {"for": 1, "against": 2}},
    ) as mocked_state:
        response = client.get(state_url)
        assert response.status_code == HTTPStatus.OK
        payload = response.json()
        assert payload["score"] == {"for": 1, "against": 2}
        assert mocked_state.call_count == 1

    with patch(
        "apps.schedule.api.views.apply_tracker_command",
        return_value={"status": "active"},
    ) as mocked_command:
        response = client.post(
            command_url,
            data={"command": "start/pause"},
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.OK
        assert response.json() == {"status": "active"}
        assert mocked_command.call_count == 1


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_tracker_command_rejects_non_object_json(client: Client) -> None:
    """Tracker command should reject JSON arrays (expects an object/dict)."""
    match = _create_match()

    coach_user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    _assign_coach(match, coach_user)
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
def test_tracker_conflict_returns_reconciliation_metadata(client: Client) -> None:
    """Conflict responses tell an offline client which command already committed."""
    match = _create_match()
    coach_user = get_user_model().objects.create_user(
        username="reconciliation-coach",
        password=TEST_PASSWORD,
    )
    _assign_coach(match, coach_user)
    client.force_login(coach_user)
    command_url = (
        f"/api/matches/{match.id_uuid}/tracker/{match.home_team.id_uuid}/commands/"
    )
    first_id = str(uuid4())
    first = client.post(
        command_url,
        data={
            "command": "start/pause",
            "command_id": first_id,
            "device_id": "api-device",
            "client_sequence": CONFLICT_CLIENT_SEQUENCE,
        },
        content_type="application/json",
    )
    assert first.status_code == HTTPStatus.OK

    conflict = client.post(
        command_url,
        data={
            "command": "start/pause",
            "command_id": str(uuid4()),
            "device_id": "api-device",
            "client_sequence": CONFLICT_CLIENT_SEQUENCE,
        },
        content_type="application/json",
    )

    assert conflict.status_code == HTTPStatus.CONFLICT
    assert conflict.json() == {
        "detail": "client_sequence was already used by another command.",
        "code": "client_sequence_conflict",
        "client_sequence": CONFLICT_CLIENT_SEQUENCE,
        "command_id": first_id,
        "committed_revision": 1,
    }


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_tracker_poll_rejects_invalid_since_revision(client: Client) -> None:
    """Tracker poll should return 400 for invalid revision cursors."""
    match = _create_match()

    coach_user = get_user_model().objects.create_user(
        username="coach",
        password=TEST_PASSWORD,
    )
    _assign_coach(match, coach_user)
    client.force_login(coach_user)

    poll_url = f"/api/matches/{match.id_uuid}/tracker/{match.home_team.id_uuid}/poll/"
    response = client.get(poll_url, {"since_revision": "not-a-revision"})
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"detail": "Invalid 'since_revision'."}
