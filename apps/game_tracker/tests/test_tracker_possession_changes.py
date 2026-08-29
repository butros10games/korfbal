"""Regression coverage for ball-loss and interception tracking."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
import pytest

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import MatchPlayer, PossessionChange, Shot
from apps.game_tracker.services.event_editor import (
    DeletePossessionChangeEvent,
    apply_event_editor_command,
)
from apps.game_tracker.services.match_stats_payload import build_match_stats_payload
from apps.game_tracker.services.match_timeline_payload import build_match_events
from apps.game_tracker.services.tracker_http import (
    TrackerCommandError,
    get_tracker_state,
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


type PossessionTracker = tuple[TrackerMatchContext, Player, Player]


@pytest.fixture
def possession_tracker() -> PossessionTracker:
    """Create a live tracker with one attacker and one defender."""
    tracker = create_tracker_match(prefix="Possession")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    create_match_part(
        match_data=tracker.match_data,
        start_offset=-timedelta(minutes=5),
    )

    group_types = create_group_types("Aanval", "Verdediging")
    attacker = create_tracker_player(username="possession_attacker")
    defender = create_tracker_player(username="possession_defender")
    attack_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Aanval"],
    )
    defense_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_types["Verdediging"],
    )
    attack_group.players.add(attacker)
    defense_group.players.add(defender)
    MatchPlayer.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        player=attacker,
    )
    MatchPlayer.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        player=defender,
    )
    return tracker, attacker, defender


@pytest.mark.django_db
def test_possession_changes_flow_through_tracker_timeline_stats_and_undo(
    possession_tracker: PossessionTracker,
) -> None:
    """Both event kinds remain player-attributed and the newest can be undone."""
    tracker, attacker, defender = possession_tracker

    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={
            "command": "possession_change_reg",
            "player_id": str(attacker.id_uuid),
            "kind": PossessionChange.BALL_LOSS,
        },
    )
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={
            "command": "possession_change_reg",
            "player_id": str(defender.id_uuid),
            "kind": PossessionChange.INTERCEPTION,
        },
    )

    state = get_tracker_state(tracker.match, team=tracker.home_team)
    players = {
        player["id"]: player
        for group in state["player_groups"]
        for player in group["players"]
    }
    assert players[str(attacker.id_uuid)]["ball_losses"] == 1
    assert players[str(attacker.id_uuid)]["interceptions"] == 0
    assert players[str(defender.id_uuid)]["ball_losses"] == 0
    assert players[str(defender.id_uuid)]["interceptions"] == 1
    assert state["last_event"]["type"] == "possession_change"
    assert state["last_event"]["kind"] == PossessionChange.INTERCEPTION
    assert state["last_event"]["player_id"] == str(defender.id_uuid)

    events = build_match_events(tracker.match_data)
    possession_events = [
        event for event in events if event["type"] == "possession_change"
    ]
    assert [event["kind"] for event in possession_events] == [
        PossessionChange.BALL_LOSS,
        PossessionChange.INTERCEPTION,
    ]
    assert [event["player_id"] for event in possession_events] == [
        str(attacker.id_uuid),
        str(defender.id_uuid),
    ]

    shot = Shot.objects.create(
        match_data=tracker.match_data,
        match_part=tracker.match_data.match_parts.get(active=True),
        team=tracker.home_team,
        player=attacker,
        for_team=True,
        scored=False,
        time=PossessionChange.objects.latest("time").time,
    )
    stats = build_match_stats_payload(
        match=tracker.match,
        match_data=tracker.match_data,
    )
    assert stats["general"]["ball_losses_for"] == 1
    assert stats["general"]["interceptions_for"] == 1
    assert stats["general"]["shots_for"] == 1
    home_players = {player["id_uuid"]: player for player in stats["players"]["home"]}
    assert home_players[str(attacker.id_uuid)]["ball_losses"] == 1
    assert home_players[str(attacker.id_uuid)]["shots_for"] == 1
    assert home_players[str(defender.id_uuid)]["interceptions"] == 1

    shot.delete()
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "remove_last_event"},
    )
    assert not PossessionChange.objects.filter(
        match_data=tracker.match_data,
        kind=PossessionChange.INTERCEPTION,
    ).exists()
    assert PossessionChange.objects.filter(
        match_data=tracker.match_data,
        kind=PossessionChange.BALL_LOSS,
    ).exists()


@pytest.mark.django_db
def test_possession_changes_can_keep_the_kind_when_the_player_is_unknown(
    possession_tracker: PossessionTracker,
) -> None:
    """Unattributed events count for the team without changing player totals."""
    tracker, attacker, defender = possession_tracker

    for kind in (PossessionChange.BALL_LOSS, PossessionChange.INTERCEPTION):
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={
                "command": "possession_change_reg",
                "kind": kind,
            },
        )

    changes = list(
        PossessionChange.objects.filter(match_data=tracker.match_data).order_by("time")
    )
    assert [change.kind for change in changes] == [
        PossessionChange.BALL_LOSS,
        PossessionChange.INTERCEPTION,
    ]
    assert [change.player_id for change in changes] == [None, None]

    state = get_tracker_state(tracker.match, team=tracker.home_team)
    players = {
        player["id"]: player
        for group in state["player_groups"]
        for player in group["players"]
    }
    assert players[str(attacker.id_uuid)]["ball_losses"] == 0
    assert players[str(defender.id_uuid)]["interceptions"] == 0
    assert state["last_event"]["kind"] == PossessionChange.INTERCEPTION
    assert state["last_event"]["player"] is None
    assert state["last_event"]["player_id"] is None

    possession_events = [
        event
        for event in build_match_events(tracker.match_data)
        if event["type"] == "possession_change"
    ]
    assert [event["player_id"] for event in possession_events] == [None, None]
    assert [event["player"] for event in possession_events] == [None, None]

    stats = build_match_stats_payload(
        match=tracker.match,
        match_data=tracker.match_data,
    )
    assert stats["general"]["ball_losses_for"] == 1
    assert stats["general"]["interceptions_for"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("player_role", "kind"),
    [
        ("Verdediging", PossessionChange.BALL_LOSS),
        ("Aanval", PossessionChange.INTERCEPTION),
    ],
)
def test_possession_change_rejects_player_in_wrong_role(
    possession_tracker: PossessionTracker,
    player_role: str,
    kind: str,
) -> None:
    """Backend role validation prevents silent tracker misattribution."""
    tracker, attacker, defender = possession_tracker
    player = defender if player_role == "Verdediging" else attacker

    with pytest.raises(TrackerCommandError) as exc:
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={
                "command": "possession_change_reg",
                "player_id": str(player.id_uuid),
                "kind": kind,
            },
        )

    assert exc.value.code == "bad_request"


@pytest.mark.django_db(transaction=True)
def test_possession_change_editor_delete_owns_revision_and_publication(
    possession_tracker: PossessionTracker,
) -> None:
    """Review deletion uses the serialized editor command boundary."""
    tracker, attacker, _defender = possession_tracker
    event = PossessionChange.objects.create(
        match_data=tracker.match_data,
        match_part=tracker.match_data.match_parts.get(active=True),
        team=tracker.home_team,
        player=attacker,
        kind=PossessionChange.BALL_LOSS,
        time=timezone.now(),
    )
    tracker.match_data.refresh_from_db()
    revision_before = tracker.match_data.live_revision
    publisher = RecordingMatchChangePublisher()

    result = apply_event_editor_command(
        match_data_id=tracker.match_data.pk,
        expected_revision=revision_before,
        actor=None,
        command=DeletePossessionChangeEvent(event_id=str(event.pk)),
        publisher=publisher,
    )

    assert result.found is True
    assert result.event is None
    assert not PossessionChange.objects.filter(pk=event.pk).exists()
    assert result.revision == revision_before + 1
    assert len(publisher.changes) == 1
