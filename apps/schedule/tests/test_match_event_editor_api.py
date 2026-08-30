"""Cross-layer contracts for match event editing."""

from datetime import timedelta
from decimal import Decimal
from http import HTTPStatus
from itertools import product
from unittest.mock import patch
from uuid import uuid4

from django.test.client import Client
from django.utils import timezone
import pytest

from apps.game_tracker.models import (
    MatchEvent,
    MatchLiveChange,
    Pause,
    PossessionChange,
    Shot,
    ShotEventDetail,
    Timeout,
)
from apps.game_tracker.services.match_impact import compute_match_impact_rows

from .match_api_test_support import (
    add_roster_player,
    create_editor_context,
    create_match_graph,
    create_match_part,
    create_user,
    goal_payload,
    login_coach,
)


JSON = "application/json"
EDITABLE_EVENT_KINDS = ("goals", "substitutes", "pauses", "timeouts")
MISSING_EVENT_MUTATIONS = [
    *product(("patch", "delete"), EDITABLE_EVENT_KINDS),
    ("delete", "possession-changes"),
]
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(("method", "event_kind"), MISSING_EVENT_MUTATIONS)
def test_missing_editor_event_does_not_publish_live_change(
    client: Client,
    method: str,
    event_kind: str,
) -> None:
    """Keep rejected missing-event mutations out of live state."""
    graph = create_match_graph(prefix=f"missing-{event_kind}-{method}")
    login_coach(client, graph, username="missing-event-coach")
    revision_before = graph.match_data.live_revision
    changes_before = MatchLiveChange.objects.filter(match_data=graph.match_data).count()
    request = client.patch if method == "patch" else client.delete

    response = request(
        f"/api/matches/{graph.match.id_uuid}/events/{event_kind}/{uuid4()}/",
        data={"expected_revision": revision_before},
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    graph.match_data.refresh_from_db()
    assert (
        graph.match_data.live_revision,
        MatchLiveChange.objects.filter(match_data=graph.match_data).count(),
    ) == (revision_before, changes_before)


def test_goal_editor_create_update_delete_flow(client: Client) -> None:
    """Keep one goal's projections and immutable history coherent."""
    context = create_editor_context(client, username="goal-lifecycle")
    graph = context.graph
    events_url = f"/api/matches/{graph.match.id_uuid}/events"
    away_user = create_user(username="away-scorer")
    away_player = add_roster_player(graph, away_user, team=graph.away_team)
    graph.match_data.status = "finished"
    graph.match_data.save(update_fields=["status"])
    graph.match_data.refresh_from_db()

    created_response = client.post(
        f"{events_url}/goals/",
        data=goal_payload(context, expected_revision=graph.match_data.live_revision),
        content_type=JSON,
    )
    assert created_response.status_code == HTTPStatus.CREATED
    created_payload = created_response.json()
    created = created_payload["event"]
    assert created_payload["live_revision"] == graph.match_data.live_revision + 1
    assert created["type"] == "goal"
    assert created["team_id"] == str(graph.home_team.id_uuid)
    assert created["player"] == "goal-lifecycle"

    shot_id = created["source_id"]
    shot = Shot.objects.get(id_uuid=shot_id)
    assert shot.for_team is True
    graph.match_data.refresh_from_db()
    assert (graph.match_data.home_score, graph.match_data.away_score) == (1, 0)

    updated_response = client.patch(
        f"{events_url}/goals/{shot_id}/",
        data={
            "for_team": True,
            "team_id": str(graph.away_team.id_uuid),
            "player_id": str(away_player.id_uuid),
            "expected_revision": created_payload["live_revision"],
        },
        content_type=JSON,
    )
    assert updated_response.status_code == HTTPStatus.OK
    updated_payload = updated_response.json()
    assert updated_payload["event"]["for_team"] is True
    shot.refresh_from_db()
    assert shot.for_team is True
    canonical = (
        ShotEventDetail.objects
        .filter(event__match_data=graph.match_data, event__source_id=shot.pk)
        .order_by("-event__sequence")
        .first()
    )
    assert canonical is not None
    assert canonical.shooter == away_player
    assert canonical.defender is None
    graph.match_data.refresh_from_db()
    assert (graph.match_data.home_score, graph.match_data.away_score) == (0, 1)

    deleted_response = client.delete(
        f"{events_url}/goals/{shot_id}/",
        data={"expected_revision": updated_payload["live_revision"]},
        content_type=JSON,
    )
    assert deleted_response.status_code == HTTPStatus.OK
    assert deleted_response.json() == {
        "event": None,
        "live_revision": updated_payload["live_revision"] + 1,
    }
    assert not Shot.objects.filter(id_uuid=shot_id).exists()
    graph.match_data.refresh_from_db()
    assert (graph.match_data.home_score, graph.match_data.away_score) == (0, 0)
    history_response = client.get(f"{events_url}/history/")
    assert history_response.status_code == HTTPStatus.OK
    shot_history = [
        event
        for event in history_response.json()["events"]
        if event["source_type"] == "shot"
    ]
    assert [event["kind"] for event in shot_history] == [
        "shot.created",
        "shot.updated",
        "shot.retracted",
    ]
    assert all(event["source"] == "editor" for event in shot_history)
    assert len({event["logical_event_id"] for event in shot_history}) == 1


def test_possession_change_editor_delete_flow(client: Client) -> None:
    """Delete an incorrectly registered possession change."""
    context = create_editor_context(client, username="possession-editor")
    graph = context.graph
    event = PossessionChange.objects.create(
        match_data=graph.match_data,
        match_part=context.match_part,
        team=graph.home_team,
        player=context.actor,
        kind=PossessionChange.BALL_LOSS,
        time=timezone.now(),
    )
    graph.match_data.refresh_from_db()
    revision_before = graph.match_data.live_revision

    response = client.delete(
        f"/api/matches/{graph.match.id_uuid}/events/possession-changes/{event.pk}/",
        data={"expected_revision": revision_before},
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "event": None,
        "live_revision": revision_before + 1,
    }
    assert not PossessionChange.objects.filter(pk=event.pk).exists()


def test_goal_editor_preserves_direct_defender_responsibility(client: Client) -> None:
    """Retain the tracked defender for an opponent goal."""
    context = create_editor_context(client, username="defending-coach")
    graph = context.graph

    response = client.post(
        f"/api/matches/{graph.match.id_uuid}/events/goals/",
        data=goal_payload(
            context,
            expected_revision=graph.match_data.live_revision,
            team_id=str(graph.away_team.id_uuid),
            for_team=False,
        ),
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.CREATED
    created = response.json()["event"]
    assert created["for_team"] is False
    assert created["team_id"] == str(graph.away_team.id_uuid)
    shot = Shot.objects.get(pk=created["source_id"])
    assert shot.player == context.actor
    assert shot.team == graph.away_team
    assert shot.for_team is False
    detail = ShotEventDetail.objects.get(event__source_id=shot.pk)
    assert (detail.shooter, detail.defender) == (None, context.actor)
    impact = next(
        row
        for row in compute_match_impact_rows(match_data=graph.match_data)
        if row.player_id == str(context.actor.id_uuid)
    )
    assert impact.team_id == str(graph.home_team.id_uuid)
    assert impact.impact_score == Decimal("-0.820")


def test_goal_editor_rejects_stale_revision(client: Client) -> None:
    """Reject a stale aggregate revision without an extra write."""
    context = create_editor_context(client, username="revision-conflict-coach")
    graph = context.graph
    expected_revision = graph.match_data.live_revision
    payload = goal_payload(context, expected_revision=expected_revision)

    first = client.post(
        f"/api/matches/{graph.match.id_uuid}/events/goals/",
        data=payload,
        content_type=JSON,
    )
    assert first.status_code == HTTPStatus.CREATED
    stale = client.post(
        f"/api/matches/{graph.match.id_uuid}/events/goals/",
        data=payload,
        content_type=JSON,
    )
    assert stale.status_code == HTTPStatus.CONFLICT
    assert stale.json() == {
        "code": "revision_conflict",
        "detail": "The match changed while you were editing.",
        "expected_revision": expected_revision,
        "live_revision": first.json()["live_revision"],
    }
    assert Shot.objects.filter(match_data=graph.match_data).count() == 1


def test_goal_editor_requires_expected_revision(client: Client) -> None:
    """Require a non-negative expected revision."""
    context = create_editor_context(client, username="missing-revision-coach")
    graph = context.graph

    response = client.post(
        f"/api/matches/{graph.match.id_uuid}/events/goals/",
        data=goal_payload(context, expected_revision=None),
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {
        "expected_revision": "A non-negative integer is required."
    }


def test_goal_editor_rejects_player_from_wrong_roster_team(client: Client) -> None:
    """Require the responsible player to belong to the selected side."""
    context = create_editor_context(client, username="roster-validation-coach")
    graph = context.graph
    revision_before = graph.match_data.live_revision

    response = client.post(
        f"/api/matches/{graph.match.id_uuid}/events/goals/",
        data=goal_payload(
            context,
            expected_revision=revision_before,
            team_id=str(graph.away_team.id_uuid),
        ),
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "player_id" in response.json()
    graph.match_data.refresh_from_db()
    assert graph.match_data.live_revision == revision_before
    assert not Shot.objects.filter(match_data=graph.match_data).exists()


def test_goal_editor_rejects_time_outside_match_part(client: Client) -> None:
    """Reject a goal after its selected period."""
    context = create_editor_context(client, username="period-validation-coach")
    graph = context.graph
    context.match_part.end_time = context.match_part.start_time + timedelta(minutes=10)
    context.match_part.active = False
    context.match_part.save(update_fields=["end_time", "active"])
    graph.match_data.refresh_from_db()
    revision_before = graph.match_data.live_revision

    response = client.post(
        f"/api/matches/{graph.match.id_uuid}/events/goals/",
        data=goal_payload(
            context,
            expected_revision=revision_before,
            minute=11,
        ),
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "time" in response.json()
    graph.match_data.refresh_from_db()
    assert graph.match_data.live_revision == revision_before
    assert not Shot.objects.filter(match_data=graph.match_data).exists()


def test_goal_editor_minute_accounts_for_completed_pauses(client: Client) -> None:
    """Map display minutes through completed pause time."""
    context = create_editor_context(client, username="pause-aware-coach")
    graph = context.graph
    Pause.objects.create(
        match_data=graph.match_data,
        match_part=context.match_part,
        start_time=context.match_part.start_time + timedelta(minutes=5),
        end_time=context.match_part.start_time + timedelta(minutes=7),
        active=False,
    )

    response = client.post(
        f"/api/matches/{graph.match.id_uuid}/events/goals/",
        data=goal_payload(
            context,
            expected_revision=graph.match_data.live_revision,
            minute=8,
        ),
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.CREATED
    shot = Shot.objects.get(pk=response.json()["event"]["source_id"])
    assert shot.time == context.match_part.start_time + timedelta(minutes=10)


@pytest.mark.django_db(transaction=True)
def test_goal_editor_rolls_back_on_envelope_failure(client: Client) -> None:
    """Commit typed event, score, envelope and revision atomically."""
    context = create_editor_context(client, username="atomic-goal-coach")
    graph = context.graph
    match_data = graph.match_data
    match_data.status = "finished"
    match_data.save(update_fields=["status"])
    match_data.refresh_from_db()
    revision_before = match_data.live_revision
    score_before = (match_data.home_score, match_data.away_score)
    changes_before = MatchLiveChange.objects.filter(match_data=match_data).count()
    events_before = MatchEvent.objects.filter(match_data=match_data).count()

    with (
        patch(
            "apps.game_tracker.services.match_events.MatchEvent.objects.create",
            side_effect=RuntimeError("forced envelope failure"),
        ),
        pytest.raises(RuntimeError, match="forced envelope failure"),
    ):
        client.post(
            f"/api/matches/{graph.match.id_uuid}/events/goals/",
            data=goal_payload(context, expected_revision=revision_before),
            content_type=JSON,
        )

    match_data.refresh_from_db()
    assert not Shot.objects.filter(match_data=match_data).exists()
    assert (match_data.home_score, match_data.away_score) == score_before
    assert match_data.live_revision == revision_before
    assert (
        MatchLiveChange.objects.filter(match_data=match_data).count() == changes_before
    )
    assert MatchEvent.objects.filter(match_data=match_data).count() == events_before


def test_timeout_editor_uses_stable_pause_event_identity(client: Client) -> None:
    """Share one durable identity between timeout deltas and routes."""
    graph = create_match_graph(prefix="timeout-editor")
    events_url = f"/api/matches/{graph.match.id_uuid}/events"
    match_part = create_match_part(graph)
    login_coach(client, graph, username="timeout-coach")
    graph.match_data.status = "active"
    graph.match_data.save(update_fields=["status"])
    graph.match_data.refresh_from_db()
    revision_before = graph.match_data.live_revision

    response = client.post(
        f"{events_url}/timeouts/",
        data={
            "team_id": str(graph.home_team.id_uuid),
            "match_part_id": str(match_part.id_uuid),
            "minute": 0,
            "length_seconds": 20,
            "expected_revision": revision_before,
        },
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.CREATED
    create_payload = response.json()
    created = create_payload["event"]
    assert created["event_kind"] == "timeout"
    assert created["source_id"] == created["pause_id"]
    assert created["event_id"] == created["logical_event_id"]
    assert created["timeout_id"] != created["pause_id"]

    delta = client.get(
        f"{events_url}/",
        {"since_revision": revision_before, "identity_version": 3},
    ).json()
    assert [event["event_id"] for event in delta["upsert"]] == [created["event_id"]]

    updated_response = client.patch(
        f"{events_url}/timeouts/{created['source_id']}/",
        data={
            "length_seconds": 30,
            "expected_revision": create_payload["live_revision"],
        },
        content_type=JSON,
    )
    assert updated_response.status_code == HTTPStatus.OK
    assert updated_response.json()["event"]["event_id"] == created["event_id"]
    timeout = Timeout.objects.get(id_uuid=created["timeout_id"])
    timeout_event = MatchEvent.objects.filter(
        match_data=graph.match_data, source_type="timeout", source_id=timeout.pk
    ).latest("sequence")
    assert timeout_event is not None
    assert created["event_id"] == str(timeout_event.logical_id)
    assert timeout.pause is not None
    assert str(timeout.pause.pk) == created["source_id"]
    pause = Pause.objects.get(id_uuid=created["pause_id"])
    assert pause.length() == timedelta(seconds=30)
