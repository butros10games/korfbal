"""Application-boundary tests for typed match-event editor commands."""

from __future__ import annotations

from uuid import uuid4

import pytest

from apps.game_tracker.models import (
    GoalType,
    MatchLiveChange,
    MatchPlayer,
    Pause,
    PlayerChange,
    Shot,
)
from apps.game_tracker.services.event_editor import (
    CreateGoalEvent,
    CreatePauseEvent,
    CreateSubstitutionEvent,
    DeleteGoalEvent,
    DeletePauseEvent,
    DeleteSubstitutionEvent,
    EventEditorValidationError,
    UpdatePauseEvent,
    UpdateSubstitutionEvent,
    apply_event_editor_command,
)
from apps.game_tracker.tests.fakes import RecordingMatchChangePublisher
from apps.game_tracker.tests.tracker_test_helpers import (
    OnCommitCapture,
    create_group_types,
    create_match_part,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)


LIFECYCLE_COMMAND_COUNT = 3
UPDATED_PAUSE_SECONDS = 30


@pytest.mark.django_db
def test_create_goal_command_commits_one_revision_and_publication(
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    """One application command owns persistence, revision, and publication."""
    tracker = create_tracker_match(prefix="Editor command")
    match_part = create_match_part(match_data=tracker.match_data)
    player = create_tracker_player(username="editor-command-player")
    MatchPlayer.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        player=player,
    )
    goal_type = GoalType.objects.create(name="Editor command goal")
    publisher = RecordingMatchChangePublisher()
    tracker.match_data.refresh_from_db()
    revision_before = tracker.match_data.live_revision

    with django_capture_on_commit_callbacks(execute=True):
        result = apply_event_editor_command(
            match_data_id=tracker.match_data.pk,
            expected_revision=revision_before,
            actor=None,
            command=CreateGoalEvent(
                player_id=player.id_uuid,
                team_id=tracker.home_team.id_uuid,
                shot_type_id=goal_type.id_uuid,
                match_part_id=match_part.id_uuid,
                time=None,
                minute=0,
                scored=True,
            ),
            publisher=publisher,
        )

    tracker.match_data.refresh_from_db()
    assert isinstance(result.event, Shot)
    assert result.event.match_data_id == tracker.match_data.id_uuid
    expected_revision = revision_before + 1
    assert tracker.match_data.live_revision == expected_revision
    assert list(
        MatchLiveChange.objects.filter(
            match_data=tracker.match_data,
            revision__gt=revision_before,
        ).values_list("revision", flat=True)
    ) == [expected_revision]
    assert len(publisher.changes) == 1
    assert publisher.changes[0].revision == expected_revision


@pytest.mark.django_db
def test_invalid_editor_command_rolls_back_without_publication() -> None:
    """Domain validation failures leave projections and revisions untouched."""
    tracker = create_tracker_match(prefix="Invalid editor command")
    match_part = create_match_part(match_data=tracker.match_data)
    player = create_tracker_player(username="invalid-editor-command-player")
    MatchPlayer.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        player=player,
    )
    goal_type = GoalType.objects.create(name="Invalid editor command goal")
    publisher = RecordingMatchChangePublisher()
    tracker.match_data.refresh_from_db()
    revision_before = tracker.match_data.live_revision

    with pytest.raises(EventEditorValidationError) as captured:
        apply_event_editor_command(
            match_data_id=tracker.match_data.pk,
            expected_revision=revision_before,
            actor=None,
            command=CreateGoalEvent(
                player_id=player.id_uuid,
                team_id=tracker.away_team.id_uuid,
                shot_type_id=goal_type.id_uuid,
                match_part_id=match_part.id_uuid,
                time=None,
                minute=0,
                scored=True,
            ),
            publisher=publisher,
        )

    tracker.match_data.refresh_from_db()
    assert "player_id" in captured.value.errors
    assert not Shot.objects.filter(match_data=tracker.match_data).exists()
    assert tracker.match_data.live_revision == revision_before
    assert publisher.changes == []


@pytest.mark.django_db
def test_substitution_commands_own_the_full_event_lifecycle(
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    """Create, correction, and deletion use one serialized command boundary."""
    tracker = create_tracker_match(prefix="Substitution editor command")
    match_part = create_match_part(match_data=tracker.match_data)
    group_type = create_group_types("Attack")["Attack"]
    player_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_type,
    )
    player_in = create_tracker_player(username="substitution-player-in")
    replacement = create_tracker_player(username="substitution-replacement")
    player_out = create_tracker_player(username="substitution-player-out")
    for player in (player_in, replacement, player_out):
        MatchPlayer.objects.create(
            match_data=tracker.match_data,
            team=tracker.home_team,
            player=player,
        )
    publisher = RecordingMatchChangePublisher()

    with django_capture_on_commit_callbacks(execute=True):
        created = apply_event_editor_command(
            match_data_id=tracker.match_data.pk,
            expected_revision=tracker.match_data.live_revision,
            actor=None,
            command=CreateSubstitutionEvent(
                player_in_id=player_in.id_uuid,
                player_out_id=player_out.id_uuid,
                player_group_id=player_group.id_uuid,
                match_part_id=match_part.id_uuid,
                time=None,
                minute=0,
            ),
            publisher=publisher,
        )
        assert isinstance(created.event, PlayerChange)

        updated = apply_event_editor_command(
            match_data_id=tracker.match_data.pk,
            expected_revision=created.revision,
            actor=None,
            command=UpdateSubstitutionEvent(
                event_id=str(created.event.pk),
                player_in_id=replacement.id_uuid,
            ),
            publisher=publisher,
        )
        assert isinstance(updated.event, PlayerChange)
        assert updated.event.player_in_id == replacement.id_uuid

        deleted = apply_event_editor_command(
            match_data_id=tracker.match_data.pk,
            expected_revision=updated.revision,
            actor=None,
            command=DeleteSubstitutionEvent(event_id=str(created.event.pk)),
            publisher=publisher,
        )
    assert deleted.found is True
    assert deleted.event is None
    assert not PlayerChange.objects.filter(pk=created.event.pk).exists()
    assert len(publisher.changes) == LIFECYCLE_COMMAND_COUNT


@pytest.mark.django_db
def test_pause_commands_own_the_full_event_lifecycle(
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    """Pause timing corrections and deletion stay inside the command envelope."""
    tracker = create_tracker_match(prefix="Pause editor command")
    match_part = create_match_part(match_data=tracker.match_data)
    publisher = RecordingMatchChangePublisher()

    with django_capture_on_commit_callbacks(execute=True):
        created = apply_event_editor_command(
            match_data_id=tracker.match_data.pk,
            expected_revision=tracker.match_data.live_revision,
            actor=None,
            command=CreatePauseEvent(
                match_part_id=match_part.id_uuid,
                start_time=None,
                minute=0,
                length_seconds=20,
                active=False,
            ),
            publisher=publisher,
        )
        assert isinstance(created.event, Pause)

        updated = apply_event_editor_command(
            match_data_id=tracker.match_data.pk,
            expected_revision=created.revision,
            actor=None,
            command=UpdatePauseEvent(
                event_id=str(created.event.pk),
                length_seconds=UPDATED_PAUSE_SECONDS,
            ),
            publisher=publisher,
        )
        assert isinstance(updated.event, Pause)
        assert updated.event.length().total_seconds() == UPDATED_PAUSE_SECONDS

        deleted = apply_event_editor_command(
            match_data_id=tracker.match_data.pk,
            expected_revision=updated.revision,
            actor=None,
            command=DeletePauseEvent(event_id=str(created.event.pk)),
            publisher=publisher,
        )
    assert deleted.found is True
    assert deleted.event is None
    assert not Pause.objects.filter(pk=created.event.pk).exists()
    assert len(publisher.changes) == LIFECYCLE_COMMAND_COUNT


@pytest.mark.django_db
def test_missing_editor_command_is_a_revision_free_no_op() -> None:
    """Missing correction targets do not produce phantom live changes."""
    tracker = create_tracker_match(prefix="Missing editor command")
    publisher = RecordingMatchChangePublisher()

    result = apply_event_editor_command(
        match_data_id=tracker.match_data.pk,
        expected_revision=tracker.match_data.live_revision,
        actor=None,
        command=DeleteGoalEvent(event_id=str(uuid4())),
        publisher=publisher,
    )

    tracker.match_data.refresh_from_db()
    assert result.found is False
    assert result.event is None
    assert tracker.match_data.live_revision == 0
    assert not MatchLiveChange.objects.filter(match_data=tracker.match_data).exists()
    assert publisher.changes == []
