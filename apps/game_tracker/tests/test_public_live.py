"""Public read consistency, cache isolation, and bounded query regression tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.cache import caches
from django.db import close_old_connections, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.game_tracker import tasks
from apps.game_tracker.composition import read_public_live
from apps.game_tracker.models import MatchData, MatchPart, Pause, Shot
from apps.game_tracker.services.tracker_commands.base import current_part
from apps.game_tracker.services.tracker_state import get_tracker_state
from apps.schedule.tests.match_api_test_support import (
    add_roster_player,
    create_match_graph,
    create_match_part,
    create_user,
)


assert tasks.publish_public_live_snapshot.name

pytestmark = pytest.mark.django_db(transaction=True)
MAX_COLD_SELECTS = 5


@pytest.mark.parametrize("state", ["upcoming", "active", "paused", "finished"])
def test_public_clock_matches_tracker_without_private_queries(state: str) -> None:
    """The optimized read preserves timer semantics and never loads a roster."""
    graph = create_match_graph(prefix=f"Public clock {state}")
    if state != "upcoming":
        part = create_match_part(graph)
        graph.match_data.status = "finished" if state == "finished" else "active"
        graph.match_data.save(update_fields=["status"])
        Pause.objects.create(
            match_data=graph.match_data,
            match_part=part,
            start_time=timezone.now() - timedelta(seconds=30),
            end_time=timezone.now() - timedelta(seconds=10),
            active=False,
        )
        if state == "paused":
            Pause.objects.create(
                match_data=graph.match_data,
                match_part=part,
                start_time=timezone.now(),
                active=True,
            )
    expected = get_tracker_state(graph.match, team=graph.home_team)
    with CaptureQueriesContext(connection) as queries:
        actual = read_public_live(match_id=graph.match.pk)
    assert actual is not None
    for key in (
        "status",
        "current_part",
        "parts",
        "paused",
        "live_revision",
        "last_changed_at",
    ):
        assert actual[key] == expected[key]
    actual["timer"].pop("server_time", None)
    expected["timer"].pop("server_time", None)
    assert actual["timer"] == expected["timer"]
    selects = [
        query for query in queries if query["sql"].lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) <= MAX_COLD_SELECTS
    sql = " ".join(query["sql"].lower() for query in queries)
    assert "for update" not in sql
    assert "playergroup" not in sql
    assert "playersong" not in sql
    assert "playerchange" not in sql


@pytest.mark.parametrize("source", ["knkv", "archive"])
def test_cached_imported_score_refreshes_at_next_revision(source: str) -> None:
    """Finished provider results retain their authoritative score on warm reads."""
    graph = create_match_graph(prefix=source)
    graph.match_data.status = "finished"
    graph.match_data.score_source = source
    graph.match_data.home_score = 12
    graph.match_data.away_score = 9
    graph.match_data.save()
    first = read_public_live(match_id=graph.match.pk)
    assert first is not None
    assert first["score"] == {"home": 12, "away": 9}
    graph.match_data.home_score = 13
    graph.match_data.save()
    second = read_public_live(match_id=graph.match.pk)
    assert second is not None
    assert second["score"] == {"home": 13, "away": 9}
    assert second["live_revision"] > first["live_revision"]


def test_warm_read_uses_no_sql_and_refreshes_server_time() -> None:
    """Shared snapshots remove score/period reads without freezing the live clock."""
    graph = create_match_graph(prefix="Warm public snapshot")
    create_match_part(graph)
    first = read_public_live(match_id=graph.match.pk, since_revision=-1)
    assert first is not None
    later = timezone.now() + timedelta(seconds=10)
    with (
        patch(
            "apps.game_tracker.services.public_live.timezone.now", return_value=later
        ),
        CaptureQueriesContext(connection) as queries,
    ):
        second = read_public_live(match_id=graph.match.pk)
    assert second is not None
    assert len(queries) == 0
    assert second["timer"]["server_time"] == later.isoformat()
    assert "resources" not in second
    assert first["timer"]["server_time"] != second["timer"]["server_time"]
    assert second["score"] == first["score"]


def test_nested_transaction_cannot_publish_rolled_back_snapshot() -> None:
    """Uncommitted reads neither consume nor populate the committed cache."""
    graph = create_match_graph(prefix="Rolled back snapshot")
    before = read_public_live(match_id=graph.match.pk)
    with transaction.atomic():
        MatchData.objects.filter(pk=graph.match_data.pk).update(status="finished")
        private = read_public_live(match_id=graph.match.pk)
        assert private is not None
        assert private["status"] == "finished"
        transaction.set_rollback(True)
    after = read_public_live(match_id=graph.match.pk)
    assert after == before


def test_missing_tracker_data_returns_none() -> None:
    """Removed match data cannot be revived by an old cached payload."""
    graph = create_match_graph(prefix="Deleted tracker")
    assert read_public_live(match_id=graph.match.pk) is not None
    graph.match_data.delete()
    assert read_public_live(match_id=graph.match.pk) is None


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="MVCC requires PostgreSQL"
)
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
@patch("apps.kwt_common.services.jobs.publish_job")
def test_concurrent_pause_cannot_mix_revisions(dispatch: Mock) -> None:
    """A committed pause during a read appears wholly in the following snapshot."""
    graph = create_match_graph(prefix="Concurrent pause snapshot")
    part = create_match_part(graph)
    graph.match_data.status = "active"
    graph.match_data.save(update_fields=["status"])
    graph.match_data.refresh_from_db()
    revision = graph.match_data.live_revision
    caches["public_live"].clear()

    def pause() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                Pause.objects.create(
                    match_data_id=graph.match_data.pk,
                    match_part_id=part.pk,
                    start_time=timezone.now(),
                    active=True,
                )
        finally:
            close_old_connections()

    def interleave(match_data: MatchData) -> MatchPart | None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(pause).result(timeout=5)
        return current_part(match_data)

    with patch(
        "apps.game_tracker.services.public_live.current_part", side_effect=interleave
    ):
        before = read_public_live(match_id=graph.match.pk)
    assert before is not None
    assert before["live_revision"] == revision
    assert before["paused"] is False
    after = read_public_live(match_id=graph.match.pk)
    assert after is not None
    assert after["live_revision"] > revision
    assert after["paused"] is True
    dispatch.assert_called()


def test_goal_and_deletion_invalidate_warm_score() -> None:
    """Native event writes replace the cached public score in the right direction."""
    graph = create_match_graph(prefix="Warm goal")
    player = add_roster_player(
        graph, create_user(username="public-score"), team=graph.home_team
    )
    before = read_public_live(match_id=graph.match.pk)
    assert before is not None
    assert before["score"] == {"home": 0, "away": 0}
    shot = Shot.objects.create(
        match_data=graph.match_data,
        team=graph.away_team,
        player=player,
        scored=True,
        time=timezone.now(),
    )
    after = read_public_live(match_id=graph.match.pk)
    assert after is not None
    assert after["score"] == {"home": 0, "away": 1}
    assert after["live_revision"] > before["live_revision"]
    shot.delete()
    deleted = read_public_live(match_id=graph.match.pk)
    assert deleted is not None
    assert deleted["score"] == before["score"]
    assert deleted["live_revision"] > after["live_revision"]
