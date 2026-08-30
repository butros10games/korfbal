"""High-value regression coverage for Korfbal data migrations."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest

from apps.game_tracker.models import MatchEvent, MatchPart
from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)


pytestmark = pytest.mark.migration_regression


@pytest.mark.django_db(transaction=True)
@pytest.mark.slow_migration
def test_mvp_model_move_preserves_award_and_vote_rows() -> None:
    """Legacy schedule-owned rows remain readable from the awards app."""
    tracker = create_tracker_match(prefix="MVP model move")
    candidate = create_tracker_player(username="migration-candidate")
    voter = create_tracker_player(username="migration-voter")
    executor = MigrationExecutor(connection)
    try:
        legacy_targets = [
            ("awards", "0001_initial"),
            ("schedule", "0006_seasonpool_match_pool"),
        ]
        executor.migrate(legacy_targets)
        legacy_apps = executor.loader.project_state(legacy_targets).apps

        legacy_award_model = legacy_apps.get_model("schedule", "MatchMvp")
        legacy_vote_model = legacy_apps.get_model("schedule", "MatchMvpVote")

        now = timezone.now()
        award = legacy_award_model.objects.create(
            match_id=tracker.match.pk,
            finished_at=now,
            closes_at=now + timedelta(days=1),
            published_at=now + timedelta(days=2),
            mvp_player_id=candidate.pk,
        )
        authenticated_vote = legacy_vote_model.objects.create(
            match_id=tracker.match.pk,
            candidate_id=candidate.pk,
            voter_id=voter.pk,
        )
        anonymous_token = uuid4()
        anonymous_vote = legacy_vote_model.objects.create(
            match_id=tracker.match.pk,
            candidate_id=candidate.pk,
            voter_token=anonymous_token,
        )

        executor = MigrationExecutor(connection)
        leaf_nodes = executor.loader.graph.leaf_nodes()
        executor.migrate(leaf_nodes)
        migrated_apps = executor.loader.project_state(leaf_nodes).apps
        award_model = migrated_apps.get_model("awards", "MatchMvp")
        vote_model = migrated_apps.get_model("awards", "MatchMvpVote")

        migrated_award = award_model.objects.get(pk=award.pk)
        assert migrated_award.match_id == tracker.match.pk
        assert migrated_award.mvp_player_id == candidate.pk
        assert migrated_award.published_at == award.published_at
        assert set(
            vote_model.objects.filter(match_id=tracker.match.pk).values_list(
                "id_uuid", flat=True
            )
        ) == {authenticated_vote.pk, anonymous_vote.pk}
        assert vote_model.objects.get(pk=authenticated_vote.pk).voter_id == voter.pk
        assert (
            vote_model.objects.get(pk=anonymous_vote.pk).voter_token == anonymous_token
        )
        with pytest.raises(LookupError):
            migrated_apps.get_model("schedule", "MatchMvp")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_event_projection_cutover_preserves_period_identity() -> None:
    """Dropping the mutable part relation retains its durable UUID."""
    tracker = create_tracker_match(prefix="Period identity migration")
    part = MatchPart.objects.create(
        match_data=tracker.match_data,
        part_number=1,
        start_time=timezone.now(),
        active=True,
    )
    event = MatchEvent.objects.get(
        match_data=tracker.match_data,
        source_type="match_part",
        source_id=part.pk,
    )

    executor = MigrationExecutor(connection)
    try:
        legacy_target = [("game_tracker", "0028_match_event_reconciliation")]
        executor.migrate(legacy_target)
        legacy_apps = executor.loader.project_state(legacy_target).apps
        legacy_event_model = legacy_apps.get_model("game_tracker", "MatchEvent")
        legacy_event_model.objects.filter(pk=event.pk).update(match_part_id=part.pk)

        executor = MigrationExecutor(connection)
        migrated_target = [("game_tracker", "0029_match_event_projection_cutover")]
        executor.migrate(migrated_target)
        migrated_apps = executor.loader.project_state(migrated_target).apps
        migrated_event_model = migrated_apps.get_model("game_tracker", "MatchEvent")

        assert migrated_event_model.objects.get(pk=event.pk).period_id == part.pk
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
@pytest.mark.slow_migration
def test_tracker_integrity_migration_normalizes_conflicting_timelines() -> None:
    """Invalid legacy parts and pauses are repaired before constraints apply."""
    tracker = create_tracker_match(prefix="Integrity migration")
    executor = MigrationExecutor(connection)
    try:
        target = [("game_tracker", "0020_matchdata_live_revision")]
        executor.migrate(target)
        legacy_apps = executor.loader.project_state(target).apps
        match_part_model = legacy_apps.get_model("game_tracker", "MatchPart")
        pause_model = legacy_apps.get_model("game_tracker", "Pause")

        now = timezone.now()
        duplicate_keeper = match_part_model.objects.create(
            match_data_id=tracker.match_data.pk,
            part_number=1,
            active=True,
            start_time=now - timedelta(minutes=10),
            end_time=now - timedelta(minutes=11),
        )
        duplicate_loser = match_part_model.objects.create(
            match_data_id=tracker.match_data.pk,
            part_number=1,
            active=False,
            start_time=now - timedelta(minutes=9),
        )
        active_keeper = match_part_model.objects.create(
            match_data_id=tracker.match_data.pk,
            part_number=2,
            active=True,
            start_time=now - timedelta(minutes=5),
        )
        invalid_active_pause = pause_model.objects.create(
            match_data_id=tracker.match_data.pk,
            active=True,
        )
        stale_active_pause = pause_model.objects.create(
            match_data_id=tracker.match_data.pk,
            active=True,
            start_time=now - timedelta(minutes=8),
        )
        active_pause = pause_model.objects.create(
            match_data_id=tracker.match_data.pk,
            active=True,
            start_time=now - timedelta(minutes=3),
        )
        reversed_pause = pause_model.objects.create(
            match_data_id=tracker.match_data.pk,
            active=False,
            start_time=now - timedelta(minutes=2),
            end_time=now - timedelta(minutes=4),
        )

        executor = MigrationExecutor(connection)
        migrated_target = [("game_tracker", "0021_tracker_integrity_constraints")]
        executor.migrate(migrated_target)
        migrated_apps = executor.loader.project_state(migrated_target).apps
        migrated_part_model = migrated_apps.get_model("game_tracker", "MatchPart")
        migrated_pause_model = migrated_apps.get_model("game_tracker", "Pause")

        assert not migrated_part_model.objects.filter(pk=duplicate_loser.pk).exists()
        duplicate_keeper = migrated_part_model.objects.get(pk=duplicate_keeper.pk)
        active_keeper = migrated_part_model.objects.get(pk=active_keeper.pk)
        assert duplicate_keeper.active is False
        assert duplicate_keeper.end_time == active_keeper.start_time
        assert active_keeper.active is True

        invalid_active_pause = migrated_pause_model.objects.get(
            pk=invalid_active_pause.pk
        )
        stale_active_pause = migrated_pause_model.objects.get(pk=stale_active_pause.pk)
        active_pause = migrated_pause_model.objects.get(pk=active_pause.pk)
        reversed_pause = migrated_pause_model.objects.get(pk=reversed_pause.pk)
        assert invalid_active_pause.active is False
        assert stale_active_pause.active is False
        assert stale_active_pause.end_time == active_pause.start_time
        assert active_pause.active is True
        assert reversed_pause.end_time == reversed_pause.start_time
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
