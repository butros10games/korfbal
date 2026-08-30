"""Audit coverage for game-tracker management command boundaries."""

from __future__ import annotations

from io import StringIO
from unittest.mock import Mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
import pytest

from apps.game_tracker.management.commands import recompute_match_impacts
from apps.game_tracker.models import MatchData, PlayerMatchMinutes, Shot
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_tracker_match,
    create_tracker_player,
)


UNKNOWN_MATCH_DATA_ID = "00000000-0000-4000-8000-000000000001"


def _set_status(
    tracker: TrackerMatchContext,
    *,
    status: str,
    home_score: int = 0,
    away_score: int = 0,
) -> MatchData:
    tracker.match_data.status = status
    tracker.match_data.home_score = home_score
    tracker.match_data.away_score = away_score
    tracker.match_data.save(update_fields=["status", "home_score", "away_score"])
    return tracker.match_data


@pytest.mark.django_db
def test_backfill_scores_dry_run_only_missing_and_repeat_are_safe() -> None:
    """Dry-run and repeat execution preserve rows outside the requested scope."""
    missing = create_tracker_match(prefix="Backfill missing")
    excluded = create_tracker_match(prefix="Backfill excluded")
    scorer = create_tracker_player(username="backfill-scorer")
    Shot.objects.create(
        player=scorer,
        match_data=missing.match_data,
        team=missing.home_team,
        scored=True,
        time=timezone.now(),
    )
    missing_data = _set_status(missing, status="finished")
    excluded_data = _set_status(
        excluded,
        status="finished",
        home_score=4,
    )

    dry_run_output = StringIO()
    call_command(
        "backfill_matchdata_scores",
        "--dry-run",
        "--only-missing",
        "--batch-size",
        "-10",
        stdout=dry_run_output,
    )

    missing_data.refresh_from_db()
    excluded_data.refresh_from_db()
    assert (missing_data.home_score, missing_data.away_score) == (0, 0)
    assert (excluded_data.home_score, excluded_data.away_score) == (4, 0)
    assert "Backfilling 1 finished matches (batch_size=1" in dry_run_output.getvalue()
    assert "Done. Processed 1 finished matches, updated 1." in dry_run_output.getvalue()

    write_output = StringIO()
    call_command(
        "backfill_matchdata_scores",
        "--only-missing",
        stdout=write_output,
    )

    missing_data.refresh_from_db()
    excluded_data.refresh_from_db()
    assert (missing_data.home_score, missing_data.away_score) == (1, 0)
    assert (excluded_data.home_score, excluded_data.away_score) == (4, 0)
    assert "Done. Processed 1 finished matches, updated 1." in write_output.getvalue()

    repeat_output = StringIO()
    call_command(
        "backfill_matchdata_scores",
        "--only-missing",
        stdout=repeat_output,
    )
    assert "No finished matches to backfill." in repeat_output.getvalue()


@pytest.mark.django_db
def test_recompute_impacts_routes_breakdown_and_compact_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command selects the requested matches and the requested persistence path."""
    finished = create_tracker_match(prefix="Impact finished")
    upcoming = create_tracker_match(prefix="Impact upcoming")
    _set_status(finished, status="finished")

    with_breakdowns = Mock(return_value=2)
    without_breakdowns = Mock(return_value=1)
    monkeypatch.setattr(
        recompute_match_impacts,
        "persist_match_impact_rows_with_breakdowns",
        with_breakdowns,
    )
    monkeypatch.setattr(
        recompute_match_impacts,
        "persist_match_impact_rows",
        without_breakdowns,
    )

    finished_output = StringIO()
    call_command("recompute_match_impacts", "--finished", stdout=finished_output)

    assert with_breakdowns.call_count == 1
    assert with_breakdowns.call_args.kwargs["match_data"].pk == finished.match_data.pk
    without_breakdowns.assert_not_called()
    assert f"{finished.match_data.id_uuid}: 2 rows" in finished_output.getvalue()
    assert "Done. Upserted 2 rows." in finished_output.getvalue()

    compact_output = StringIO()
    call_command(
        "recompute_match_impacts",
        "--match-data-id",
        str(upcoming.match_data.id_uuid),
        "--skip-breakdowns",
        stdout=compact_output,
    )

    assert without_breakdowns.call_count == 1
    compact_match_data = without_breakdowns.call_args.kwargs["match_data"]
    assert compact_match_data.pk == upcoming.match_data.pk
    assert f"{upcoming.match_data.id_uuid}: 1 rows" in compact_output.getvalue()
    assert "Done. Upserted 1 rows." in compact_output.getvalue()


@pytest.mark.django_db
def test_recompute_commands_without_a_selector_do_not_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operational backfills require an explicit match or finished-match scope."""
    persist_impact = Mock(return_value=1)
    persist_minutes = Mock(return_value=1)
    monkeypatch.setattr(
        recompute_match_impacts,
        "persist_match_impact_rows_with_breakdowns",
        persist_impact,
    )
    monkeypatch.setattr(
        "apps.game_tracker.management.commands.recompute_match_minutes.persist_match_minutes",
        persist_minutes,
    )

    for command in ("recompute_match_impacts", "recompute_match_minutes"):
        stdout = StringIO()
        stderr = StringIO()
        call_command(command, stdout=stdout, stderr=stderr)
        assert not stdout.getvalue()
        assert stderr.getvalue() == "Provide --match-data-id or --finished\n"

    persist_impact.assert_not_called()
    persist_minutes.assert_not_called()


@pytest.mark.django_db
def test_recompute_minutes_only_missing_honors_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only-missing excludes current rows before applying the processing limit."""
    missing_one = create_tracker_match(prefix="Minutes missing one")
    missing_two = create_tracker_match(prefix="Minutes missing two")
    current = create_tracker_match(prefix="Minutes current")
    for tracker in (missing_one, missing_two, current):
        _set_status(tracker, status="finished")

    player = create_tracker_player(username="minutes-current-player")
    PlayerMatchMinutes.objects.create(
        match_data=current.match_data,
        player=player,
        minutes_played="10.00",
    )
    processed_ids: list[str] = []

    def persist(*, match_data: MatchData) -> int:
        processed_ids.append(str(match_data.id_uuid))
        return 1

    monkeypatch.setattr(
        "apps.game_tracker.management.commands.recompute_match_minutes.persist_match_minutes",
        persist,
    )
    output = StringIO()

    call_command(
        "recompute_match_minutes",
        "--finished",
        "--only-missing",
        "--limit",
        "1",
        stdout=output,
    )

    assert len(processed_ids) == 1
    assert processed_ids[0] in {
        str(missing_one.match_data.id_uuid),
        str(missing_two.match_data.id_uuid),
    }
    assert str(current.match_data.id_uuid) not in processed_ids
    assert "Done. Processed 1 matches; upserted 1 rows." in output.getvalue()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--limit", "0"), "--limit must be at least 1"),
        (
            ("--old-version", "v8", "--new-version", "v8"),
            "--old-version and --new-version must differ",
        ),
        (
            ("--match-data-id", UNKNOWN_MATCH_DATA_ID),
            "No matching match data found",
        ),
    ],
)
@pytest.mark.django_db
def test_compare_impacts_rejects_invalid_or_empty_selections(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    """Invalid comparisons fail explicitly instead of emitting empty reports."""
    with pytest.raises(CommandError, match=message):
        call_command("compare_match_impacts", *arguments)
