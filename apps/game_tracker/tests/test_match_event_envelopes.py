"""Regression coverage for append-only match event envelopes."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.utils import timezone
import pytest

from apps.game_tracker.models import MatchEvent, Pause, Shot
from apps.game_tracker.services.match_event_context import match_event_context
from apps.game_tracker.services.match_timeline_payload import build_match_shots
from apps.game_tracker.services.tracker_http import apply_tracker_command
from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
    create_tracker_user,
)


@pytest.mark.django_db
def test_typed_updates_append_versions_and_retractions() -> None:
    """Corrections retain their prior version and deletion appends a retraction."""
    tracker = create_tracker_match(prefix="Event versions")
    player = create_tracker_player(username="event-version-player")
    command_id = uuid4()

    with match_event_context(
        source_team=tracker.home_team,
        command_id=command_id,
    ):
        shot = Shot.objects.create(
            player=player,
            match_data=tracker.match_data,
            team=tracker.home_team,
            scored=False,
            time=timezone.now(),
        )
        shot_id = shot.id_uuid
        shot.scored = True
        shot.save(update_fields=["scored"])
        shot.delete()

    events = list(
        MatchEvent.objects.filter(
            match_data=tracker.match_data,
            source_type="shot",
            source_id=shot_id,
        ).order_by("sequence")
    )
    assert [event.kind for event in events] == [
        "shot.created",
        "shot.updated",
        "shot.retracted",
    ]
    assert [event.status for event in events] == [
        MatchEvent.STATUS_SUPERSEDED,
        MatchEvent.STATUS_RETRACTED,
        MatchEvent.STATUS_ACTIVE,
    ]
    assert events[1].supersedes == events[0]
    assert events[2].supersedes == events[1]
    assert events[0].payload["record"]["scored"] is False
    assert events[1].payload["record"]["scored"] is True
    assert all(event.command_id == command_id for event in events)


@pytest.mark.django_db
def test_timeline_uses_commit_sequence_instead_of_client_time() -> None:
    """Backdated writes retain their unambiguous committed event order."""
    tracker = create_tracker_match(prefix="Event order")
    player = create_tracker_player(username="event-order-player")
    now = timezone.now()
    first = Shot.objects.create(
        player=player,
        match_data=tracker.match_data,
        team=tracker.home_team,
        scored=False,
        time=now,
    )
    second = Shot.objects.create(
        player=player,
        match_data=tracker.match_data,
        team=tracker.home_team,
        scored=False,
        time=now - timedelta(minutes=5),
    )

    shots = build_match_shots(tracker.match_data)
    assert [shot["event_id"] for shot in shots] == [
        str(first.id_uuid),
        str(second.id_uuid),
    ]
    assert [shot["event_sequence"] for shot in shots] == [1, 2]


@pytest.mark.django_db
def test_tracker_command_attributes_events_to_actor_and_team() -> None:
    """Typed writes inherit command identity and authenticated attribution."""
    tracker = create_tracker_match(prefix="Event actor")
    actor = create_tracker_user(username="event-actor")
    command_id = uuid4()

    state = apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        actor=actor,
        payload={
            "command": "start/pause",
            "command_id": str(command_id),
        },
    )

    event = MatchEvent.objects.get(match_data=tracker.match_data)
    assert event.kind == "match_part.started"
    assert event.sequence == 1
    assert event.elapsed_ms == 0
    assert event.actor == actor
    assert event.source_team == tracker.home_team
    assert event.command_id == command_id
    assert state["status"] == "active"


@pytest.mark.django_db
def test_undo_uses_committed_sequence_for_pause_resume() -> None:
    """A pause resume is newer than facts recorded while the clock was paused."""
    tracker = create_tracker_match(prefix="Event undo order")
    player = create_tracker_player(username="event-undo-player")

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )
    pause = Pause.objects.get(match_data=tracker.match_data)
    shot = Shot.objects.create(
        player=player,
        match_data=tracker.match_data,
        match_part=pause.match_part,
        team=tracker.home_team,
        scored=False,
        time=timezone.now(),
    )
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "remove_last_event"},
    )

    pause.refresh_from_db()
    assert pause.active is True
    assert pause.end_time is None
    assert Shot.objects.filter(pk=shot.pk).exists()


@pytest.mark.django_db
def test_match_deletion_cascades_versioned_event_history() -> None:
    """A match can still be removed after events supersede earlier versions."""
    tracker = create_tracker_match(prefix="Event cascade")
    player = create_tracker_player(username="event-cascade-player")
    shot = Shot.objects.create(
        player=player,
        match_data=tracker.match_data,
        team=tracker.home_team,
        scored=False,
        time=timezone.now(),
    )
    shot.scored = True
    shot.save(update_fields=["scored"])
    match_data_id = tracker.match_data.id_uuid

    tracker.match.delete()

    assert MatchEvent.objects.filter(match_data_id=match_data_id).exists() is False
