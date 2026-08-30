"""Audit coverage for tracker service and command transaction contracts."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import (
    MatchLiveChange,
    MatchPlayer,
    Pause,
    PlayerChange,
    Shot,
    TrackerCommand,
)
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.services.live_updates import (
    record_match_change,
    summarize_match_changes,
)
from apps.game_tracker.services.tracker_commands import (
    TrackerCommandError,
    command_definition,
)
from apps.game_tracker.tests.fakes import RecordingMatchChangePublisher
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_group_types,
    create_match_part,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import Player


type SubstitutionContext = tuple[TrackerMatchContext, Player, Player]

UNKNOWN_PLAYER_ID = "00000000-0000-4000-8000-000000000002"


def _parse_command(payload: dict[str, object]) -> object:
    definition = command_definition(payload)
    return definition.parse(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"command": 1},
        {"command": "unknown"},
        {"command": "timeout"},
        {"command": "shot_reg", "player_id": "player", "for_team": "yes"},
        {
            "command": "shot_reg",
            "player_id": "player",
            "for_team": True,
            "shot_type": 1,
        },
        {"command": "goal_reg", "player_id": "player", "for_team": True},
        {"command": "possession_change_reg", "kind": "turnover"},
        {
            "command": "possession_change_reg",
            "kind": "ball_loss",
            "player_id": 1,
        },
        {"command": "substitute_reg", "new_player_id": "player"},
    ],
)
def test_registry_rejects_invalid_public_command_payloads(
    payload: dict[str, object],
) -> None:
    """Every public parser translates malformed payloads to the shared contract."""
    with pytest.raises(TrackerCommandError) as error:
        _parse_command(payload)

    assert error.value.code == "bad_request"


@pytest.fixture
def live_substitution_context() -> SubstitutionContext:
    """Create a live match with one active and one reserve player."""
    tracker = create_tracker_match(prefix="Audit substitution")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    create_match_part(
        match_data=tracker.match_data,
        start_offset=-timedelta(minutes=5),
    )
    group_types = create_group_types("Aanval", "Reserve")
    active_player = create_tracker_player(username="audit-sub-active")
    reserve_player = create_tracker_player(username="audit-sub-reserve")
    active_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Aanval"],
    )
    reserve_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Reserve"],
    )
    active_group.players.add(active_player)
    reserve_group.players.add(reserve_player)
    return tracker, active_player, reserve_player


@pytest.mark.django_db
@pytest.mark.parametrize("invalid_player_id", ["not-a-uuid", UNKNOWN_PLAYER_ID])
def test_substitution_rejects_malformed_or_unknown_player_without_partial_write(
    live_substitution_context: SubstitutionContext,
    invalid_player_id: str,
) -> None:
    """Bad player identifiers use the public error contract and roll back receipts."""
    tracker, active_player, _reserve_player = live_substitution_context
    command_id = str(uuid4())
    tracker.match_data.refresh_from_db()
    revision_before = tracker.match_data.live_revision

    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={
                "command": "substitute_reg",
                "command_id": command_id,
                "new_player_id": invalid_player_id,
                "old_player_id": str(active_player.id_uuid),
            },
        )

    assert error.value.code == "bad_request"
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.command_sequence == 0
    assert tracker.match_data.live_revision == revision_before
    assert not TrackerCommand.objects.filter(command_id=command_id).exists()
    assert not PlayerChange.objects.filter(match_data=tracker.match_data).exists()


@pytest.mark.django_db
def test_substitution_rejects_player_outside_active_lineup_without_partial_write(
    live_substitution_context: SubstitutionContext,
) -> None:
    """A roster player who is not active cannot be substituted out."""
    tracker, _active_player, reserve_player = live_substitution_context
    second_reserve = create_tracker_player(username="audit-sub-second-reserve")
    reserve_group = tracker.match_data.player_groups.get(
        team=tracker.home_team,
        starting_type__name="Reserve",
    )
    reserve_group.players.add(second_reserve)
    command_id = str(uuid4())

    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={
                "command": "substitute_reg",
                "command_id": command_id,
                "new_player_id": str(reserve_player.id_uuid),
                "old_player_id": str(second_reserve.id_uuid),
            },
        )

    assert error.value.code == "bad_request"
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.command_sequence == 0
    assert not TrackerCommand.objects.filter(command_id=command_id).exists()
    assert not PlayerChange.objects.filter(match_data=tracker.match_data).exists()


@pytest.mark.django_db
def test_substitution_rejects_incoming_player_outside_match_roster(
    live_substitution_context: SubstitutionContext,
) -> None:
    """An arbitrary existing player cannot be inserted into the active lineup."""
    tracker, active_player, _reserve_player = live_substitution_context
    outsider = create_tracker_player(username="audit-sub-outsider")
    command_id = str(uuid4())

    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={
                "command": "substitute_reg",
                "command_id": command_id,
                "new_player_id": str(outsider.id_uuid),
                "old_player_id": str(active_player.id_uuid),
            },
        )

    assert error.value.code == "bad_request"
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.command_sequence == 0
    assert not TrackerCommand.objects.filter(command_id=command_id).exists()
    assert not PlayerChange.objects.filter(match_data=tracker.match_data).exists()


@pytest.mark.django_db
def test_live_play_command_without_active_part_uses_specific_error_contract() -> None:
    """A started aggregate with no active part reports no_active_part, not paused."""
    tracker = create_tracker_match(prefix="Audit missing active part")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    command_id = str(uuid4())

    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={
                "command": "new_attack",
                "command_id": command_id,
            },
        )

    assert error.value.code == "no_active_part"
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.command_sequence == 0
    assert not TrackerCommand.objects.filter(command_id=command_id).exists()


@pytest.mark.django_db
def test_failed_command_rolls_back_receipt_sequence_and_domain_writes() -> None:
    """Validation after receipt creation remains atomic with all command state."""
    tracker = create_tracker_match(prefix="Audit command rollback")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    create_match_part(match_data=tracker.match_data)
    outsider = create_tracker_player(username="audit-command-outsider")
    command_id = str(uuid4())
    tracker.match_data.refresh_from_db()
    revision_before = tracker.match_data.live_revision
    live_change_count_before = MatchLiveChange.objects.filter(
        match_data=tracker.match_data
    ).count()

    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={
                "command": "shot_reg",
                "command_id": command_id,
                "player_id": str(outsider.id_uuid),
                "for_team": True,
            },
        )

    assert error.value.code == "bad_request"
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.command_sequence == 0
    assert tracker.match_data.live_revision == revision_before
    assert not TrackerCommand.objects.filter(command_id=command_id).exists()
    assert not Shot.objects.filter(match_data=tracker.match_data).exists()
    assert (
        MatchLiveChange.objects.filter(match_data=tracker.match_data).count()
        == live_change_count_before
    )


@pytest.mark.django_db
def test_non_mutating_command_does_not_consume_revision_or_idempotency_key() -> None:
    """Legacy read commands return state without creating mutation bookkeeping."""
    tracker = create_tracker_match(prefix="Audit read command")
    command_id = str(uuid4())

    state = apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={
            "command": "get_non_active_players",
            "command_id": command_id,
            "expected_revision": 0,
        },
    )

    tracker.match_data.refresh_from_db()
    assert state["resources"] == []
    assert state["command_sequence"] == 0
    assert state["live_revision"] == 0
    assert tracker.match_data.command_sequence == 0
    assert tracker.match_data.live_revision == 0
    assert not TrackerCommand.objects.filter(command_id=command_id).exists()


@pytest.mark.django_db
def test_command_id_cannot_be_replayed_against_a_different_aggregate() -> None:
    """Global idempotency keys remain bound to their original match and team."""
    first = create_tracker_match(prefix="Audit command scope first")
    second = create_tracker_match(prefix="Audit command scope second")
    command_id = str(uuid4())
    payload = {"command": "start/pause", "command_id": command_id}
    apply_tracker_command(first.match, team=first.home_team, payload=payload)

    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(second.match, team=second.home_team, payload=payload)

    assert error.value.code == "idempotency_conflict"
    assert error.value.details == {"command_id": command_id}
    second.match_data.refresh_from_db()
    assert second.match_data.command_sequence == 0
    assert second.match_data.live_revision == 0
    assert TrackerCommand.objects.filter(command_id=command_id).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "metadata",
    [
        {"expected_revision": True},
        {"expected_revision": -1},
        {"client_sequence": 0},
        {"client_sequence": True, "device_id": "device"},
        {"client_source": " "},
        {"device_id": "x" * 129},
    ],
)
def test_invalid_command_metadata_is_rejected_before_state_changes(
    metadata: dict[str, object],
) -> None:
    """Malformed concurrency metadata never consumes aggregate sequencing."""
    tracker = create_tracker_match(prefix=f"Audit metadata {uuid4()}")

    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={"command": "start/pause", **metadata},
        )

    assert error.value.code == "bad_request"
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.command_sequence == 0
    assert tracker.match_data.live_revision == 0
    assert not TrackerCommand.objects.filter(match_data=tracker.match_data).exists()


@pytest.mark.django_db
def test_start_pause_clamps_pause_end_to_its_start_time() -> None:
    """An out-of-order server clock cannot persist a negative pause interval."""
    tracker = create_tracker_match(prefix="Audit pause clock")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    match_part = create_match_part(
        match_data=tracker.match_data,
        start_offset=-timedelta(minutes=10),
    )
    future_start = match_part.start_time + timedelta(hours=1)
    pause = Pause.objects.create(
        match_data=tracker.match_data,
        match_part=match_part,
        start_time=future_start,
        active=True,
    )

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "start/pause"},
    )

    pause.refresh_from_db()
    assert pause.active is False
    assert pause.end_time == future_start


@pytest.mark.django_db(transaction=True)
def test_empty_live_resource_change_is_a_true_no_op() -> None:
    """Callers cannot create invisible revisions or publications."""
    tracker = create_tracker_match(prefix="Audit empty revision")
    publisher = RecordingMatchChangePublisher()

    revision = record_match_change(
        tracker.match_data,
        resources=set(),
        publisher=publisher,
    )

    tracker.match_data.refresh_from_db()
    assert revision == 0
    assert tracker.match_data.live_revision == 0
    assert not MatchLiveChange.objects.filter(match_data=tracker.match_data).exists()
    assert publisher.changes == []


@pytest.mark.django_db(transaction=True)
def test_change_summary_falls_back_when_revision_history_has_a_gap() -> None:
    """A missing durable revision forces consumers to refresh every resource."""
    tracker = create_tracker_match(prefix="Audit revision gap")
    publisher = RecordingMatchChangePublisher()
    record_match_change(
        tracker.match_data,
        resources={LiveResource.EVENTS},
        changed_ids={LiveResource.EVENTS: {"event-1"}},
        publisher=publisher,
    )
    record_match_change(
        tracker.match_data,
        resources={LiveResource.SHOTS},
        changed_ids={LiveResource.SHOTS: {"shot-2"}},
        publisher=publisher,
    )
    MatchLiveChange.objects.filter(
        match_data=tracker.match_data,
        revision=1,
    ).delete()
    tracker.match_data.refresh_from_db()

    summary = summarize_match_changes(tracker.match_data, since_revision=0)

    assert summary.history_complete is False
    assert summary.resources == frozenset(LiveResource)
    assert summary.changed_ids == {}
    assert summary.complete_id_resources == frozenset()


@pytest.mark.django_db
def test_registered_match_player_can_record_shot_without_group_assignment() -> None:
    """A roster snapshot is sufficient even before lineup groups are assigned."""
    tracker = create_tracker_match(prefix="Audit roster shot")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    create_match_part(match_data=tracker.match_data)
    player = create_tracker_player(username="audit-roster-shot")
    MatchPlayer.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        player=player,
    )

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={
            "command": "shot_reg",
            "player_id": str(player.id_uuid),
            "for_team": True,
        },
    )

    shot = Shot.objects.get(match_data=tracker.match_data)
    assert shot.player == player
    assert shot.team == tracker.home_team
    assert shot.for_team is True
