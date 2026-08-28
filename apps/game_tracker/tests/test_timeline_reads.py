"""Read-side consistency and query-bound regressions for match timelines."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import timedelta
from typing import Any, NoReturn, Self

from django.db import connection
from django.db.models import QuerySet
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.game_tracker.models import (
    GoalType,
    MatchPlayer,
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)
from apps.game_tracker.services import timeline_reads
from apps.game_tracker.services.timeline_reads import (
    consistent_timeline_read,
    read_match_event_history,
    read_match_events,
    read_match_shots,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_group_types,
    create_match_part,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)


EXPANDED_EVENT_COUNT = 8
EVENT_QUERY_LIMIT = 12
SHOT_QUERY_LIMIT = 10
HISTORY_QUERY_LIMIT = 6
POSTGRES_READ_SNAPSHOT_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)


class _RecordingCursor:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


class _PostgresConnection:
    vendor = "postgresql"
    in_atomic_block = False

    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.statements)


def _query_count(read: Callable[[], object]) -> tuple[int, list[str]]:
    with CaptureQueriesContext(connection) as captured:
        read()
    return len(captured), [query["sql"] for query in captured.captured_queries]


def _create_timeline() -> tuple[TrackerMatchContext, dict[str, Any]]:
    tracker = create_tracker_match(prefix="Timeline reads")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    match_part = create_match_part(
        match_data=tracker.match_data,
        start_offset=-timedelta(minutes=20),
    )
    goal_type = GoalType.objects.create(name="Timeline read goal")
    player = create_tracker_player(username="timeline-read-player")
    replacement = create_tracker_player(username="timeline-read-replacement")
    for roster_player in (player, replacement):
        MatchPlayer.objects.create(
            match_data=tracker.match_data,
            team=tracker.home_team,
            player=roster_player,
        )
    group_type = create_group_types("Timeline attack")["Timeline attack"]
    player_group = create_player_group(
        match_data=tracker.match_data,
        team=tracker.home_team,
        group_type=group_type,
    )
    player_group.players.add(player, replacement)
    event_time = timezone.now() - timedelta(minutes=5)
    Shot.objects.create(
        match_data=tracker.match_data,
        match_part=match_part,
        player=player,
        team=tracker.home_team,
        shot_type=goal_type,
        time=event_time,
        scored=True,
    )
    PlayerChange.objects.create(
        match_data=tracker.match_data,
        match_part=match_part,
        player_group=player_group,
        player_in=replacement,
        player_out=player,
        time=event_time,
    )
    pause = Pause.objects.create(
        match_data=tracker.match_data,
        match_part=match_part,
        start_time=event_time - timedelta(minutes=1),
        end_time=event_time,
        active=False,
    )
    Timeout.objects.create(
        match_data=tracker.match_data,
        match_part=match_part,
        pause=pause,
        team=tracker.home_team,
    )
    return tracker, {
        "match_part": match_part,
        "goal_type": goal_type,
        "player": player,
        "replacement": replacement,
        "player_group": player_group,
    }


def _expand_timeline(tracker: TrackerMatchContext, fixture: dict[str, Any]) -> None:
    event_time = timezone.now() - timedelta(minutes=3)
    for index in range(EXPANDED_EVENT_COUNT):
        offset = timedelta(seconds=index)
        Shot.objects.create(
            match_data=tracker.match_data,
            match_part=fixture["match_part"],
            player=fixture["player"],
            team=tracker.home_team,
            shot_type=fixture["goal_type"],
            time=event_time + offset,
            scored=index % 2 == 0,
        )
        PlayerChange.objects.create(
            match_data=tracker.match_data,
            match_part=fixture["match_part"],
            player_group=fixture["player_group"],
            player_in=fixture["replacement"],
            player_out=fixture["player"],
            time=event_time + offset,
        )
        pause = Pause.objects.create(
            match_data=tracker.match_data,
            match_part=fixture["match_part"],
            start_time=event_time + offset,
            end_time=event_time + offset + timedelta(seconds=1),
            active=False,
        )
        Timeout.objects.create(
            match_data=tracker.match_data,
            match_part=fixture["match_part"],
            pause=pause,
            team=tracker.home_team,
        )


def test_postgres_timeline_reads_use_a_read_only_repeatable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production reads use MVCC consistency without row-level write locks."""
    fake_connection = _PostgresConnection()
    monkeypatch.setattr(timeline_reads, "connection", fake_connection)
    monkeypatch.setattr(timeline_reads.transaction, "atomic", nullcontext)

    with consistent_timeline_read():
        pass

    assert fake_connection.statements == [POSTGRES_READ_SNAPSHOT_SQL]


@pytest.mark.django_db
def test_timeline_query_counts_do_not_grow_with_event_volume() -> None:
    """Timeline and history queries stay bounded as projections accumulate."""
    tracker, fixture = _create_timeline()
    match_data_id = tracker.match_data.pk

    event_count, event_sql = _query_count(
        lambda: read_match_events(
            match_data_id=match_data_id,
            since_revision=None,
            current_identity=False,
        )
    )
    shot_count, shot_sql = _query_count(
        lambda: read_match_shots(
            match_data_id=match_data_id,
            since_revision=None,
            current_identity=False,
        )
    )
    history_count, history_sql = _query_count(
        lambda: read_match_event_history(match_data_id=match_data_id)
    )

    _expand_timeline(tracker, fixture)

    expanded_event_count, _ = _query_count(
        lambda: read_match_events(
            match_data_id=match_data_id,
            since_revision=None,
            current_identity=False,
        )
    )
    expanded_shot_count, _ = _query_count(
        lambda: read_match_shots(
            match_data_id=match_data_id,
            since_revision=None,
            current_identity=False,
        )
    )
    expanded_history_count, _ = _query_count(
        lambda: read_match_event_history(match_data_id=match_data_id)
    )

    assert expanded_event_count == event_count
    assert expanded_shot_count == shot_count
    assert expanded_history_count == history_count
    assert event_count <= EVENT_QUERY_LIMIT
    assert shot_count <= SHOT_QUERY_LIMIT
    assert history_count <= HISTORY_QUERY_LIMIT
    assert not any(
        "FOR UPDATE" in sql.upper() for sql in [*event_sql, *shot_sql, *history_sql]
    )


@pytest.mark.django_db
def test_timeline_read_boundary_never_requests_a_write_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET read models must remain independent from aggregate write locks."""
    tracker, _fixture = _create_timeline()

    def reject_write_lock(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("timeline read requested select_for_update")

    monkeypatch.setattr(QuerySet, "select_for_update", reject_write_lock)

    read_match_events(
        match_data_id=tracker.match_data.pk,
        since_revision=None,
        current_identity=False,
    )
    read_match_shots(
        match_data_id=tracker.match_data.pk,
        since_revision=None,
        current_identity=False,
    )
    read_match_event_history(match_data_id=tracker.match_data.pk)
