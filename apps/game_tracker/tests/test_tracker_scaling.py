"""Repeatable latency samples and query bounds for growing tracker timelines."""

from time import perf_counter
from uuid import uuid4

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import Shot
from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)


QUERY_COUNT_TOLERANCE = 2


@pytest.mark.django_db
def test_tracker_command_queries_remain_bounded_as_timeline_grows() -> None:
    """Report timings without a hardware-dependent latency assertion."""
    tracker = create_tracker_match(prefix="Scaling")
    player = create_tracker_player(username="scaling-player")
    apply_tracker_command(
        tracker.match, team=tracker.home_team, payload={"command": "start/pause"}
    )
    part = tracker.match_data.match_parts.get(active=True)
    query_counts = []
    previous = 0
    for size in (10, 100, 300):
        for _ in range(size - previous):
            Shot.objects.create(
                match_data=tracker.match_data,
                match_part=part,
                team=tracker.home_team,
                player=player,
                for_team=True,
                scored=False,
                time=timezone.now(),
            )
        previous = size
        with CaptureQueriesContext(connection) as queries:
            started = perf_counter()
            response = apply_tracker_command(
                tracker.match,
                team=tracker.home_team,
                payload={"command": "start/pause", "command_id": str(uuid4())},
            )
            elapsed = perf_counter() - started
        query_counts.append(len(queries))
        print(
            f"tracker timeline={size} command_ms={elapsed * 1000:.1f} "
            f"queries={len(queries)}"
        )
        assert response["live_revision"] > 0
    assert max(query_counts) - min(query_counts) <= QUERY_COUNT_TOLERANCE
