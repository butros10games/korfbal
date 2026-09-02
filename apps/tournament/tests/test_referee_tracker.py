"""Permission, concurrency, and publication coverage for the referee tracker."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from http import HTTPStatus
from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
import pytest

from apps.tournament.composition import change_publisher
from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentMember,
    TournamentResultAudit,
    TournamentStage,
    TournamentTeam,
)


pytestmark = pytest.mark.django_db
OnCommitCapture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
REVISION_AFTER_HOME_GOAL = 2
EXPECTED_GOAL_AUDITS = 2
EXPECTED_TOURNAMENT_REVISION = 3


def _match_graph() -> tuple[
    object,
    object,
    Tournament,
    TournamentField,
    TournamentField,
    TournamentMatch,
    TournamentMatch,
]:
    user_model = get_user_model()
    manager = user_model.objects.create_user(username="referee-manager")
    referee = user_model.objects.create_user(username="field-referee")
    tournament = Tournament.objects.create(
        name="Referee Cup",
        slug="referee-cup",
        owner=manager,
        starts_at=timezone.now(),
        status=Tournament.Status.PUBLISHED,
    )
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Pools",
        kind=TournamentStage.Kind.POOL,
    )
    field_one = TournamentField.objects.create(tournament=tournament, label="Field 1")
    field_two = TournamentField.objects.create(tournament=tournament, label="Field 2")
    teams = [
        TournamentTeam.objects.create(tournament=tournament, name=f"Team {index}")
        for index in range(1, 5)
    ]
    match_one = TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        field=field_one,
        home_team=teams[0],
        away_team=teams[1],
        match_number=1,
    )
    match_two = TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        field=field_two,
        home_team=teams[2],
        away_team=teams[3],
        match_number=2,
    )
    TournamentMember.objects.create(
        tournament=tournament,
        user=referee,
        role=TournamentMember.Role.SCOREKEEPER,
        field=field_one,
    )
    return manager, referee, tournament, field_one, field_two, match_one, match_two


def test_referee_tracker_requires_authentication_and_assigned_field(
    client: Client,
) -> None:
    """The focused tracker remains private and respects field scope."""
    _, referee, _, _, _, allowed_match, denied_match = _match_graph()

    anonymous = client.get(f"/api/tournaments/matches/{allowed_match.id_uuid}/tracker/")
    assert anonymous.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
    assert "Location" not in anonymous.headers

    client.force_login(referee)
    allowed = client.get(f"/api/tournaments/matches/{allowed_match.id_uuid}/tracker/")
    denied = client.get(f"/api/tournaments/matches/{denied_match.id_uuid}/tracker/")

    assert allowed.status_code == HTTPStatus.OK
    assert allowed.json()["match"]["field"]["label"] == "Field 1"
    assert allowed.json()["match"]["home_team"]["name"] == "Team 1"
    assert denied.status_code == HTTPStatus.FORBIDDEN


def test_referee_marks_ready_and_each_goal_is_published_once(
    client: Client,
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    """Readiness and score commands advance revisions without duplicate goals."""
    _, referee, tournament, _, _, match, _ = _match_graph()
    client.force_login(referee)
    ready_url = f"/api/tournaments/matches/{match.id_uuid}/tracker/ready/"
    goal_url = f"/api/tournaments/matches/{match.id_uuid}/tracker/goal/"

    capture_callbacks = django_capture_on_commit_callbacks
    with (
        patch.object(change_publisher, "publish") as publish,
        capture_callbacks(execute=True),
    ):
        ready = client.post(
            ready_url,
            data={"expected_revision": 0},
            content_type="application/json",
        )
        repeated_ready = client.post(
            ready_url,
            data={"expected_revision": 0},
            content_type="application/json",
        )
        home_goal = client.post(
            goal_url,
            data={"side": "home", "expected_revision": 1},
            content_type="application/json",
        )
        stale_retry = client.post(
            goal_url,
            data={"side": "home", "expected_revision": 1},
            content_type="application/json",
        )
        away_goal = client.post(
            goal_url,
            data={"side": "away", "expected_revision": 2},
            content_type="application/json",
        )

    assert ready.status_code == HTTPStatus.OK
    assert ready.json()["match"]["field_ready_at"] is not None
    assert ready.json()["match"]["status"] == TournamentMatch.Status.SCHEDULED
    assert ready.json()["match"]["revision"] == 1
    assert repeated_ready.status_code == HTTPStatus.OK
    assert repeated_ready.json()["match"]["revision"] == 1
    assert home_goal.status_code == HTTPStatus.OK
    assert home_goal.json()["match"]["home_score"] == 1
    assert home_goal.json()["match"]["away_score"] == 0
    assert home_goal.json()["match"]["status"] == TournamentMatch.Status.LIVE
    assert home_goal.json()["match"]["revision"] == REVISION_AFTER_HOME_GOAL
    assert stale_retry.status_code == HTTPStatus.CONFLICT
    assert stale_retry.json()["state"]["match"]["home_score"] == 1
    assert away_goal.status_code == HTTPStatus.OK
    assert away_goal.json()["match"]["home_score"] == 1
    assert away_goal.json()["match"]["away_score"] == 1
    assert (
        TournamentResultAudit.objects.filter(match=match).count()
        == EXPECTED_GOAL_AUDITS
    )

    match.refresh_from_db()
    tournament.refresh_from_db()
    assert match.field_ready_by_id == referee.pk
    assert tournament.status == Tournament.Status.LIVE
    assert tournament.live_revision == EXPECTED_TOURNAMENT_REVISION
    assert publish.call_args_list == [
        call(tournament_id=str(tournament.id_uuid), revision=1),
        call(tournament_id=str(tournament.id_uuid), revision=2),
        call(tournament_id=str(tournament.id_uuid), revision=3),
    ]


def test_referee_goal_requires_readiness_and_open_match(client: Client) -> None:
    """Goals cannot bypass readiness or alter a finalized result."""
    manager, _, _, _, _, match, _ = _match_graph()
    client.force_login(manager)
    goal_url = f"/api/tournaments/matches/{match.id_uuid}/tracker/goal/"

    before_ready = client.post(
        goal_url,
        data={"side": "away", "expected_revision": 0},
        content_type="application/json",
    )
    assert before_ready.status_code == HTTPStatus.CONFLICT
    assert before_ready.json()["state"]["match"]["away_score"] is None

    match.status = TournamentMatch.Status.FINAL
    match.home_score = 3
    match.away_score = 2
    match.save(update_fields=["status", "home_score", "away_score"])
    final_ready = client.post(
        f"/api/tournaments/matches/{match.id_uuid}/tracker/ready/",
        data={"expected_revision": 0},
        content_type="application/json",
    )
    assert final_ready.status_code == HTTPStatus.CONFLICT
    assert TournamentResultAudit.objects.filter(match=match).count() == 0


def test_public_snapshot_includes_operational_readiness_without_actor(
    client: Client,
) -> None:
    """Displays receive readiness while the referee identity stays private."""
    _, referee, tournament, _, _, match, _ = _match_graph()
    match.field_ready_at = timezone.now()
    match.field_ready_by = referee
    match.save(update_fields=["field_ready_at", "field_ready_by"])

    response = client.get(f"/api/tournaments/public/{tournament.slug}/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()["matches"][0]
    assert payload["field_ready_at"] is not None
    assert "field_ready_by" not in payload
