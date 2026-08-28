"""Application-boundary tests for typed player designation commands."""

from __future__ import annotations

from typing import Any, cast

import pytest

from apps.game_tracker.models import MatchLiveChange, MatchPlayer, PlayerGroup
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.services.player_designation import (
    DesignatePlayersCommand,
    PlayerDesignationSelection,
    PlayerDesignationValidationError,
    apply_player_designation,
)
from apps.game_tracker.tests.fakes import RecordingMatchChangePublisher
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_tracker_match,
    create_tracker_player,
    create_tracker_user,
)


def _staff_actor(*, username: str) -> object:
    actor = cast(Any, create_tracker_user(username=username))
    actor.is_staff = True
    actor.save(update_fields=["is_staff"])
    return actor


@pytest.mark.django_db(transaction=True)
def test_designation_command_commits_roster_revision_and_publication() -> None:
    """One command owns the lineup write and its observable side effects."""
    tracker = create_tracker_match(prefix="Designation command")
    create_group_types("Reserve")
    reserve = PlayerGroup.objects.get(
        match_data=tracker.match_data,
        team=tracker.home_team,
        starting_type__name="Reserve",
    )
    player = create_tracker_player(username="designation-player")
    publisher = RecordingMatchChangePublisher()

    result = apply_player_designation(
        actor=_staff_actor(username="designation-editor"),
        command=DesignatePlayersCommand(
            players=(PlayerDesignationSelection(player_id=str(player.id_uuid)),),
            target_group_id=str(reserve.id_uuid),
            expected_revision=tracker.match_data.live_revision,
        ),
        publisher=publisher,
    )

    tracker.match_data.refresh_from_db()
    assert reserve.players.filter(pk=player.pk).exists()
    assert MatchPlayer.objects.filter(
        match_data=tracker.match_data,
        team=tracker.home_team,
        player=player,
    ).exists()
    assert result.revision == tracker.match_data.live_revision == 1
    assert list(
        MatchLiveChange.objects.filter(match_data=tracker.match_data).values_list(
            "revision",
            flat=True,
        )
    ) == [1]
    assert len(publisher.changes) == 1
    assert publisher.changes[0].resources == {
        LiveResource.TRACKER,
        LiveResource.PLAYER_GROUPS,
        LiveResource.STATS,
        LiveResource.IMPACTS,
    }


@pytest.mark.django_db(transaction=True)
def test_invalid_batch_rolls_back_all_designations() -> None:
    """A later invalid move cannot leave earlier players partially moved."""
    tracker = create_tracker_match(prefix="Designation rollback")
    create_group_types("Aanval", "Verdediging", "Reserve")
    groups = {
        group.starting_type.name: group
        for group in PlayerGroup.objects.filter(
            match_data=tracker.match_data,
            team=tracker.home_team,
        ).select_related("starting_type")
    }
    valid_player = create_tracker_player(username="valid-designation-player")
    invalid_player = create_tracker_player(username="invalid-designation-player")
    groups["Reserve"].players.add(valid_player)
    groups["Verdediging"].players.add(invalid_player)
    publisher = RecordingMatchChangePublisher()

    with pytest.raises(PlayerDesignationValidationError):
        apply_player_designation(
            actor=_staff_actor(username="rollback-editor"),
            command=DesignatePlayersCommand(
                players=(
                    PlayerDesignationSelection(
                        player_id=str(valid_player.id_uuid),
                        source_group_id=str(groups["Reserve"].id_uuid),
                    ),
                    PlayerDesignationSelection(
                        player_id=str(invalid_player.id_uuid),
                        source_group_id=str(groups["Verdediging"].id_uuid),
                    ),
                ),
                target_group_id=str(groups["Aanval"].id_uuid),
                expected_revision=tracker.match_data.live_revision,
            ),
            publisher=publisher,
        )

    tracker.match_data.refresh_from_db()
    assert groups["Reserve"].players.filter(pk=valid_player.pk).exists()
    assert not groups["Aanval"].players.filter(pk=valid_player.pk).exists()
    assert groups["Verdediging"].players.filter(pk=invalid_player.pk).exists()
    assert tracker.match_data.live_revision == 0
    assert not MatchLiveChange.objects.filter(match_data=tracker.match_data).exists()
    assert publisher.changes == []


@pytest.mark.django_db(transaction=True)
def test_capacity_failure_is_revision_free() -> None:
    """Capacity validation runs inside the command without partial side effects."""
    tracker = create_tracker_match(prefix="Designation capacity")
    create_group_types("Aanval")
    attack = PlayerGroup.objects.get(
        match_data=tracker.match_data,
        team=tracker.home_team,
        starting_type__name="Aanval",
    )
    existing = [
        create_tracker_player(username=f"capacity-existing-{index}")
        for index in range(4)
    ]
    attack.players.add(*existing)
    candidate = create_tracker_player(username="capacity-candidate")
    publisher = RecordingMatchChangePublisher()

    with pytest.raises(
        PlayerDesignationValidationError,
        match="Too many players selected",
    ):
        apply_player_designation(
            actor=_staff_actor(username="capacity-editor"),
            command=DesignatePlayersCommand(
                players=(PlayerDesignationSelection(player_id=str(candidate.id_uuid)),),
                target_group_id=str(attack.id_uuid),
                expected_revision=tracker.match_data.live_revision,
            ),
            publisher=publisher,
        )

    tracker.match_data.refresh_from_db()
    assert not attack.players.filter(pk=candidate.pk).exists()
    assert tracker.match_data.live_revision == 0
    assert publisher.changes == []
