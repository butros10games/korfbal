"""Exercise command serialization on independent PostgreSQL connections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from django.db import close_old_connections, connection
import pytest

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import MatchPart, TrackerCommand
from apps.game_tracker.services.tracker_http import TrackerCommandError
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_match


pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
    ),
]


@pytest.mark.parametrize("duplicate", [False, True])
def test_simultaneous_commands_serialize_without_duplicate_transitions(
    duplicate: bool,
) -> None:
    """Concurrent retries commit once; competing stale edits conflict."""
    tracker = create_tracker_match(prefix="Concurrent commands")
    barrier = Barrier(2, timeout=10)
    command_id = str(uuid4())

    def submit() -> str:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
            barrier.wait()
            try:
                apply_tracker_command(
                    tracker.match,
                    team=tracker.home_team,
                    payload={
                        "command": "start/pause",
                        "command_id": command_id if duplicate else str(uuid4()),
                        "expected_revision": 0,
                    },
                )
            except TrackerCommandError as error:
                return error.code
            return "ok"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert sorted(results) == (
        ["ok", "ok"] if duplicate else ["ok", "revision_conflict"]
    )
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.live_revision == 1
    assert TrackerCommand.objects.filter(match_data=tracker.match_data).count() == 1
    assert MatchPart.objects.filter(match_data=tracker.match_data).count() == 1
