"""Pruning the empty lineups every imported match used to receive."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Event
from time import sleep
from unittest.mock import patch

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.db.models.expressions import Combinable
import pytest

from apps.game_tracker.models import PlayerGroup
from apps.game_tracker.services import player_groups
from apps.game_tracker.services.match_mutations import locked_match_mutation
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_tracker_match,
    create_tracker_player,
    get_tracker_group,
)
from apps.kwt_common.models import BackgroundJob


pytestmark = pytest.mark.django_db

GROUPS_PER_MATCH = 4
ALL_GROUPS = 3 * GROUPS_PER_MATCH


def test_prune_keeps_used_and_upcoming_lineups() -> None:
    """Only whole, never-used lineups of started matches are removed."""
    empty = create_tracker_match(prefix="Empty", start_offset=-timedelta(days=3))
    used = create_tracker_match(prefix="Used", start_offset=-timedelta(days=3))
    upcoming = create_tracker_match(prefix="Upcoming", start_offset=timedelta(days=2))
    create_group_types("Aanval", "Reserve")
    get_tracker_group(used, "Reserve", used.away_team).players.add(
        create_tracker_player(username="away-reserve")
    )

    dry_run = StringIO()
    call_command("prune_unused_player_groups", "--dry-run", stdout=dry_run)
    assert dry_run.getvalue().strip() == "Would delete 4 player groups of 1 matches."
    assert PlayerGroup.objects.count() == ALL_GROUPS

    output = StringIO()
    jobs = BackgroundJob.objects.count()
    call_command("prune_unused_player_groups", "--batch-size", "1", stdout=output)
    # Removing empty groups changes no statistics, so it queues no recomputes.
    assert BackgroundJob.objects.count() == jobs

    assert output.getvalue().strip() == "Deleted 4 player groups of 1 matches."
    assert not PlayerGroup.objects.filter(match_data=empty.match_data).exists()
    assert (
        PlayerGroup.objects.filter(match_data=used.match_data).count()
        == GROUPS_PER_MATCH
    )
    assert (
        PlayerGroup.objects.filter(match_data=upcoming.match_data).count()
        == GROUPS_PER_MATCH
    )
    # A pruned lineup is recreated on its next use.
    assert get_tracker_group(empty, "Reserve").match_data_id == empty.match_data.pk


def test_prune_rechecks_lineups_saved_after_its_first_check() -> None:
    """A lineup saved between the unlocked scan and the delete is retained."""
    tracker = create_tracker_match(prefix="Saved", start_offset=-timedelta(days=3))
    create_group_types("Aanval", "Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    first_check = player_groups._unused_lineups
    calls = 0

    def editor_saves_after_scan(batch: list[object], used: Combinable) -> list[object]:
        nonlocal calls
        calls += 1
        unused = first_check(batch, used)
        if calls == 1:
            reserve.players.add(create_tracker_player(username="late-reserve"))
        return unused

    with patch.object(player_groups, "_unused_lineups", editor_saves_after_scan):
        call_command("prune_unused_player_groups", stdout=StringIO())

    assert PlayerGroup.objects.filter(match_data=tracker.match_data).count() == (
        GROUPS_PER_MATCH
    )
    assert reserve.players.exists()


@pytest.mark.service_backed
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
)
def test_prune_waits_for_a_concurrent_lineup_write() -> None:
    """On separate connections, the editor's uncommitted save blocks the delete."""
    tracker = create_tracker_match(prefix="Racing", start_offset=-timedelta(days=3))
    create_group_types("Aanval", "Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    player = create_tracker_player(username="racing-reserve")
    written, pruning = Event(), Event()

    def editor() -> None:
        close_old_connections()
        try:
            with locked_match_mutation(tracker.match_data.pk):
                reserve.players.add(player)
                written.set()
                assert pruning.wait(timeout=10)
                # Let the cleanup scan, see no committed players and reach the lock.
                sleep(0.5)
        finally:
            connection.close()

    def prune() -> str:
        close_old_connections()
        try:
            assert written.wait(timeout=10)
            pruning.set()
            output = StringIO()
            call_command("prune_unused_player_groups", stdout=output)
            return output.getvalue().strip()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        saving = executor.submit(editor)
        pruned = executor.submit(prune)
        saving.result(timeout=20)
        assert pruned.result(timeout=20) == "Deleted 0 player groups of 0 matches."

    assert PlayerGroup.objects.filter(match_data=tracker.match_data).count() == (
        GROUPS_PER_MATCH
    )
    assert list(reserve.players.all()) == [player]
