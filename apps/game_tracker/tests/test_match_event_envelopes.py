"""Regression coverage for append-only match event envelopes."""

from __future__ import annotations

from datetime import timedelta
from importlib import import_module
from uuid import uuid4

from django.apps import apps as django_apps
from django.db import IntegrityError
from django.utils import timezone
import pytest

from apps.game_tracker.models import (
    Attack,
    MatchData,
    MatchEvent,
    MatchEventObservation,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
    ShotEventDetail,
    SubstitutionEventDetail,
    Timeout,
)
from apps.game_tracker.services.lineup_projections import (
    capture_starting_lineup,
    rebuild_match_projections,
)
from apps.game_tracker.services.match_event_context import (
    match_event_context,
    suppress_match_event_recording,
)
from apps.game_tracker.services.match_event_replay import (
    IncompleteMatchEventHistoryError,
    rebuild_typed_event_projections,
)
from apps.game_tracker.services.match_events import build_match_event_history
from apps.game_tracker.services.match_timeline_payload import build_match_shots
from apps.game_tracker.services.tracker_http import apply_tracker_command
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
    create_tracker_user,
)


def _convert_historical_events(match_data: MatchData) -> None:
    """Emulate and validate conversion of pre-snapshot canonical envelopes."""
    MatchEvent.objects.filter(match_data=match_data).update(
        payload={"operation": "created", "backfilled": True},
        elapsed_ms=None,
    )
    migration = import_module(
        "apps.game_tracker.migrations.0030_backfill_historical_event_snapshots"
    )
    migration.backfill_historical_event_snapshots(django_apps, None)
    assert all(
        event.payload.get("record")
        for event in MatchEvent.objects.filter(match_data=match_data)
    )
    assert not MatchEvent.objects.filter(
        match_data=match_data,
        period_id__isnull=False,
        effective_at__isnull=False,
        elapsed_ms__isnull=True,
    ).exists()
    assert all(
        observation.payload.get("record")
        for observation in MatchEventObservation.objects.filter(
            match_data=match_data,
            origin=MatchEventObservation.ORIGIN_CANONICAL,
        )
    )


@pytest.mark.django_db
def test_canonical_event_migration_assigns_one_logical_id_per_root() -> None:
    """Historical roots do not inherit Django's one-off AddField default."""
    tracker = create_tracker_match(prefix="Event logical migration")
    shared_default = uuid4()
    first_source = uuid4()
    second_source = uuid4()
    MatchEvent.objects.bulk_create([
        MatchEvent(
            match_data=tracker.match_data,
            sequence=1,
            logical_id=shared_default,
            kind="attack.created",
            source_type="attack",
            source_id=first_source,
            payload={},
        ),
        MatchEvent(
            match_data=tracker.match_data,
            sequence=2,
            logical_id=shared_default,
            kind="attack.updated",
            source_type="attack",
            source_id=first_source,
            payload={},
        ),
        MatchEvent(
            match_data=tracker.match_data,
            sequence=3,
            logical_id=shared_default,
            kind="attack.created",
            source_type="attack",
            source_id=second_source,
            payload={},
        ),
    ])

    migration = import_module(
        "apps.game_tracker.migrations.0026_canonical_event_details"
    )
    migration.backfill_canonical_event_fields(django_apps, None)

    events = list(
        MatchEvent.objects.filter(match_data=tracker.match_data).order_by("sequence")
    )
    assert events[0].logical_id == events[1].logical_id
    assert events[0].logical_id != events[2].logical_id
    assert shared_default not in {event.logical_id for event in events}


@pytest.mark.django_db
def test_observation_migration_backfills_historical_envelopes() -> None:
    """Every pre-observation audit envelope retains one original report."""
    tracker = create_tracker_match(prefix="Observation migration")
    Attack.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        time=timezone.now(),
    )
    event = MatchEvent.objects.get(match_data=tracker.match_data)
    MatchEventObservation.objects.filter(event=event).delete()

    migration = import_module(
        "apps.game_tracker.migrations.0028_match_event_reconciliation"
    )
    migration.backfill_event_observations(django_apps, None)

    observation = MatchEventObservation.objects.get(event=event)
    assert observation.match_data == tracker.match_data
    assert observation.origin == MatchEventObservation.ORIGIN_CANONICAL
    assert observation.payload == event.payload


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
    assert events[1].supersedes == events[0]
    assert events[2].supersedes == events[1]
    assert len({event.logical_id for event in events}) == 1
    assert events[0].payload["record"]["scored"] is False
    assert events[1].payload["record"]["scored"] is True
    assert all(event.command_id == command_id for event in events)
    assert list(
        ShotEventDetail.objects
        .filter(event__in=events)
        .order_by("event__sequence")
        .values_list("outcome", flat=True)
    ) == ["miss", "goal", "goal"]
    assert [
        event["status"] for event in build_match_event_history(tracker.match_data)
    ] == [
        MatchEvent.STATUS_SUPERSEDED,
        MatchEvent.STATUS_SUPERSEDED,
        MatchEvent.STATUS_RETRACTED,
    ]


@pytest.mark.django_db(transaction=True)
def test_projection_failure_rolls_back_the_event_appended_first() -> None:
    """A failed typed projection cannot leave a canonical fact without its row."""
    tracker = create_tracker_match(prefix="Event-first rollback")
    now = timezone.now()
    MatchPart.objects.create(
        match_data=tracker.match_data,
        part_number=1,
        start_time=now,
        active=False,
    )

    with pytest.raises(IntegrityError):
        MatchPart.objects.create(
            match_data=tracker.match_data,
            part_number=1,
            start_time=now + timedelta(minutes=1),
            active=False,
        )

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.event_sequence == 1
    assert MatchEvent.objects.filter(match_data=tracker.match_data).count() == 1
    assert MatchPart.objects.filter(match_data=tracker.match_data).count() == 1


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
    assert [shot["source_id"] for shot in shots] == [
        str(first.id_uuid),
        str(second.id_uuid),
    ]
    assert [shot["event_sequence"] for shot in shots] == [1, 2]
    assert all(shot["logical_event_id"] for shot in shots)


@pytest.mark.django_db
def test_substitution_versions_have_canonical_relational_details() -> None:
    """Substitution envelopes retain team, group, and both player roles."""
    tracker = create_tracker_match(prefix="Substitution details")
    player_out = create_tracker_player(username="detail-player-out")
    player_in = create_tracker_player(username="detail-player-in")
    group_type = create_group_types("Aanval")["Aanval"]
    group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_type,
    )

    change = PlayerChange.objects.create(
        match_data=tracker.match_data,
        player_group=group,
        player_out=player_out,
        player_in=player_in,
        time=timezone.now(),
    )

    detail = SubstitutionEventDetail.objects.get(
        event__source_type="player_change",
        event__source_id=change.pk,
    )
    assert detail.team == tracker.home_team
    assert detail.player_group == group
    assert detail.player_out == player_out
    assert detail.player_in == player_in


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
    assert event.period_id is not None
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


@pytest.mark.django_db
def test_audit_history_includes_misses_attacks_and_prior_versions() -> None:
    """The ordered history exposes every fact, not only public timeline goals."""
    tracker = create_tracker_match(prefix="Complete history")
    player = create_tracker_player(username="history-player")
    event_time = timezone.now()
    shot = Shot.objects.create(
        player=player,
        match_data=tracker.match_data,
        team=tracker.home_team,
        scored=False,
        time=event_time,
    )
    shot.scored = True
    shot.save(update_fields=["scored"])
    Attack.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        time=event_time,
    )

    history = build_match_event_history(tracker.match_data)

    assert [event["kind"] for event in history] == [
        "shot.created",
        "shot.updated",
        "attack.created",
    ]
    assert history[0]["detail"]["outcome"] == "miss"
    assert history[0]["status"] == MatchEvent.STATUS_SUPERSEDED
    assert history[1]["detail"]["outcome"] == "goal"
    assert history[2]["detail"] is None
    assert all(len(event["observations"]) == 1 for event in history)


@pytest.mark.django_db
def test_typed_projections_rebuild_exactly_without_appending_events() -> None:
    """Every operational event table and aggregate can be replayed from envelopes."""
    tracker = create_tracker_match(prefix="Projection replay")
    player_out = create_tracker_player(username="replay-player-out")
    player_in = create_tracker_player(username="replay-player-in")
    group_types = create_group_types("Aanval", "Reserve")
    group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Aanval"],
    )
    reserve = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Reserve"],
    )
    group.players.add(player_out)
    reserve.players.add(player_in)
    capture_starting_lineup(tracker.match_data)

    now = timezone.now()
    part = MatchPart.objects.create(
        match_data=tracker.match_data,
        part_number=1,
        start_time=now - timedelta(minutes=5),
        end_time=None,
        active=True,
    )
    pause = Pause.objects.create(
        match_data=tracker.match_data,
        match_part=part,
        start_time=now - timedelta(minutes=2),
        end_time=now - timedelta(minutes=1),
        active=False,
    )
    timeout = Timeout.objects.create(
        match_data=tracker.match_data,
        match_part=part,
        pause=pause,
        team=tracker.home_team,
    )
    shot = Shot.objects.create(
        player=player_out,
        match_data=tracker.match_data,
        match_part=part,
        team=tracker.home_team,
        scored=True,
        time=now - timedelta(seconds=30),
    )
    change = PlayerChange.objects.create(
        match_data=tracker.match_data,
        match_part=part,
        player_group=group,
        player_out=player_out,
        player_in=player_in,
        time=now - timedelta(seconds=20),
    )
    attack = Attack.objects.create(
        match_data=tracker.match_data,
        match_part=part,
        team=tracker.away_team,
        time=now - timedelta(seconds=10),
    )
    rebuild_match_projections(tracker.match_data)

    # Reproduce envelopes produced for matches that predate snapshot payloads.
    _convert_historical_events(tracker.match_data)

    expected = {
        "part": part.pk,
        "pause": pause.pk,
        "timeout": timeout.pk,
        "shot": shot.pk,
        "change": change.pk,
        "attack": attack.pk,
    }
    event_count = MatchEvent.objects.filter(match_data=tracker.match_data).count()
    tracker.match_data.refresh_from_db()
    last_sequence = tracker.match_data.event_sequence

    with suppress_match_event_recording():
        Timeout.objects.filter(match_data=tracker.match_data).delete()
        Shot.objects.filter(match_data=tracker.match_data).delete()
        PlayerChange.objects.filter(match_data=tracker.match_data).delete()
        Attack.objects.filter(match_data=tracker.match_data).delete()
        Pause.objects.filter(match_data=tracker.match_data).delete()
        MatchPart.objects.filter(pk=part.pk).update(
            start_time=now,
            active=False,
        )
        tracker.match_data.home_score = 99
        tracker.match_data.away_score = 98
        tracker.match_data.save(update_fields=["home_score", "away_score"])
        group.players.clear()
        reserve.players.set([player_out, player_in])

    rebuild_typed_event_projections(tracker.match_data)
    # Replaying an already-correct projection is also a no-op for the event log.
    rebuild_typed_event_projections(tracker.match_data)

    tracker.match_data.refresh_from_db()
    part.refresh_from_db()
    assert (
        MatchPart.objects.get(pk=expected["part"]).active,
        Pause.objects.get(pk=expected["pause"]).end_time,
        Timeout.objects.get(pk=expected["timeout"]).pause_id,
        Shot.objects.get(pk=expected["shot"]).scored,
        PlayerChange.objects.get(pk=expected["change"]).player_in,
        Attack.objects.get(pk=expected["attack"]).team,
    ) == (
        True,
        pause.end_time,
        expected["pause"],
        True,
        player_in,
        tracker.away_team,
    )
    assert tracker.match_data.home_score == 1
    assert tracker.match_data.away_score == 0
    assert set(group.players.values_list("pk", flat=True)) == {player_in.pk}
    assert set(reserve.players.values_list("pk", flat=True)) == {player_out.pk}
    assert (
        MatchEvent.objects.filter(match_data=tracker.match_data).count()
        == event_count
    )
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.event_sequence == last_sequence


@pytest.mark.django_db
def test_replay_refuses_incomplete_history_before_deleting_projections() -> None:
    """An unconverted historical envelope cannot cause destructive replay."""
    tracker = create_tracker_match(prefix="Incomplete replay")
    attack = Attack.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        time=timezone.now(),
    )
    MatchEvent.objects.filter(
        match_data=tracker.match_data,
        source_type="attack",
    ).update(payload={"operation": "created", "backfilled": True})

    with pytest.raises(IncompleteMatchEventHistoryError):
        rebuild_typed_event_projections(tracker.match_data)

    assert Attack.objects.filter(pk=attack.pk).exists()


@pytest.mark.django_db
def test_historical_conversion_fails_if_an_active_projection_is_missing() -> None:
    """Deployment stops instead of silently blessing unrecoverable active facts."""
    tracker = create_tracker_match(prefix="Unrecoverable history")
    attack = Attack.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        time=timezone.now(),
    )
    MatchEvent.objects.filter(
        match_data=tracker.match_data,
        source_type="attack",
    ).update(payload={"operation": "created", "backfilled": True})
    with suppress_match_event_recording():
        attack.delete()

    migration = import_module(
        "apps.game_tracker.migrations.0030_backfill_historical_event_snapshots"
    )
    with pytest.raises(RuntimeError, match="missing projections"):
        migration.backfill_historical_event_snapshots(django_apps, None)
