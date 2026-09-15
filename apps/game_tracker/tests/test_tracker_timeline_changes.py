"""Scoring deltas match full timeline diffs without constructing them in the lock."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection
from django.utils import timezone
import pytest

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import (
    GoalType,
    MatchLiveChange,
    MatchPlayer,
    Shot,
    TrackerCommand,
)
from apps.game_tracker.services.match_timeline_payload import (
    build_match_timeline_payloads,
)
from apps.game_tracker.services.tracker_commands.base import TrackerCommandError
from apps.game_tracker.services.tracker_commands.scoring import ShotCommand
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_match_part,
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import Player
from apps.schedule.models import Match


pytestmark = pytest.mark.django_db


@dataclass
class ScoringFixture:
    """An active match with a selectable player on each side."""

    tracker: TrackerMatchContext
    home_player: Player
    away_player: Player
    goal_type: GoalType

    def payload(
        self, *, command: str = "goal_reg", for_team: bool = True
    ) -> dict[str, Any]:
        """Return a fresh, explicitly timed client command."""
        return {
            "command": command,
            "command_id": str(uuid4()),
            "player_id": str(self.home_player.pk),
            "for_team": for_team,
            "goal_type": str(self.goal_type.pk),
            "client_time_ms": int(timezone.now().timestamp() * 1000),
        }


@pytest.fixture
def scoring() -> ScoringFixture:
    """Create native lineup membership without depending on a clock boundary."""
    tracker = create_tracker_match(prefix="Explicit delta")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    create_match_part(match_data=tracker.match_data, start_offset=-timedelta(minutes=3))
    home, away = (
        create_tracker_player(username=name) for name in ("delta-home", "delta-away")
    )
    for player, team in ((home, tracker.home_team), (away, tracker.away_team)):
        MatchPlayer.objects.create(
            match_data=tracker.match_data, player=player, team=team
        )
    return ScoringFixture(
        tracker, home, away, GoalType.objects.create(name="Delta goal")
    )


def _full_rows(scoring: ScoringFixture) -> dict[str, dict[str, dict[str, Any]]]:
    events, shots = build_match_timeline_payloads(scoring.tracker.match_data)
    return {
        name: {row["event_id"]: row for row in rows}
        for name, rows in (("events", events), ("shots", shots))
    }


def _assert_delta(
    scoring: ScoringFixture, before: dict[str, dict[str, dict[str, Any]]], revision: int
) -> None:
    after = _full_rows(scoring)
    change = MatchLiveChange.objects.get(
        match_data=scoring.tracker.match_data, revision=revision
    )
    for resource, rows in before.items():
        expected = {
            key
            for key in rows.keys() | after[resource].keys()
            if rows.get(key) != after[resource].get(key)
        }
        assert set(change.changed_ids[resource]) == expected


@pytest.mark.parametrize("command", ["shot_reg", "goal_reg"])
@pytest.mark.parametrize("for_team", [False, True])
@pytest.mark.parametrize("backdated", [False, True])
def test_explicit_deltas_equal_full_diff(
    scoring: ScoringFixture, command: str, for_team: bool, backdated: bool
) -> None:
    """New and backdated shots preserve exact logical IDs in both perspectives."""
    tracker = scoring.tracker
    for prior in ("goal_reg", "shot_reg", "goal_reg"):
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload=scoring.payload(command=prior),
        )
    before = _full_rows(scoring)
    payload = scoring.payload(command=command, for_team=for_team)
    if backdated:
        payload["client_time_ms"] -= 10_000
    with patch(
        "apps.game_tracker.services.tracker_http._timeline_resource_payloads",
        side_effect=AssertionError("full timeline built inside scoring command"),
    ):
        result = apply_tracker_command(
            tracker.match, team=tracker.home_team, payload=payload
        )
    _assert_delta(scoring, before, result["live_revision"])


@pytest.mark.parametrize("command", ["shot_reg", "goal_reg"])
def test_reconciled_report_has_empty_deltas(
    scoring: ScoringFixture, command: str
) -> None:
    """A matched opposite-team report must not claim another canonical event."""
    tracker = scoring.tracker
    payload = scoring.payload(command=command)
    first = apply_tracker_command(
        tracker.match, team=tracker.home_team, payload=payload
    )
    before = _full_rows(scoring)
    payload.update(
        command_id=str(uuid4()), player_id=str(scoring.away_player.pk), for_team=False
    )
    with patch(
        "apps.game_tracker.services.tracker_http._timeline_resource_payloads",
        side_effect=AssertionError("full timeline built for matched report"),
    ):
        second = apply_tracker_command(
            tracker.match, team=tracker.away_team, payload=payload
        )
    assert Shot.objects.filter(match_data=tracker.match_data).count() == 1
    assert second["live_revision"] > first["live_revision"]
    _assert_delta(scoring, before, second["live_revision"])
    change = MatchLiveChange.objects.get(
        match_data=tracker.match_data, revision=second["live_revision"]
    )
    assert change.changed_ids == {"events": [], "shots": []}


def test_replay_and_stale_revision_preserve_scoring_integrity(
    scoring: ScoringFixture,
) -> None:
    """Fast delta reporting leaves receipts and optimistic concurrency intact."""
    tracker = scoring.tracker
    tracker.match_data.refresh_from_db()
    payload = scoring.payload()
    payload["expected_revision"] = tracker.match_data.live_revision
    first = apply_tracker_command(
        tracker.match, team=tracker.home_team, payload=payload
    )
    assert (
        apply_tracker_command(tracker.match, team=tracker.home_team, payload=payload)
        == first
    )
    payload["command_id"] = str(uuid4())
    with pytest.raises(TrackerCommandError, match="Tracker state changed"):
        apply_tracker_command(tracker.match, team=tracker.home_team, payload=payload)
    assert Shot.objects.filter(match_data=tracker.match_data).count() == 1


def test_missing_delta_rolls_back_receipt(scoring: ScoringFixture) -> None:
    """A broken optimized handler cannot commit an invented empty timeline delta."""
    tracker = scoring.tracker
    tracker.match_data.refresh_from_db()
    sequence = tracker.match_data.command_sequence
    with (
        patch.object(ShotCommand, "apply", return_value=None),
        pytest.raises(RuntimeError, match="did not report"),
    ):
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload=scoring.payload(command="shot_reg"),
        )
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.command_sequence == sequence
    assert not TrackerCommand.objects.filter(match_data=tracker.match_data).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Row locks require PostgreSQL"
)
@pytest.mark.parametrize("same_command", [False, True])
def test_concurrent_scoring_retains_revision_and_receipt_guards(
    scoring: ScoringFixture, same_command: bool
) -> None:
    """Simultaneous commands either replay one receipt or reject the stale writer."""
    scoring.tracker.match_data.refresh_from_db()
    first = scoring.payload()
    first["expected_revision"] = scoring.tracker.match_data.live_revision
    second = dict(first)
    if not same_command:
        second["command_id"] = str(uuid4())
    barrier = Barrier(2)

    def submit(payload: dict[str, Any]) -> dict[str, Any] | str:
        close_old_connections()
        try:
            match = Match.objects.select_related(
                "home_team__club", "away_team__club", "season"
            ).get(pk=scoring.tracker.match.pk)
            barrier.wait(timeout=5)
            try:
                return apply_tracker_command(
                    match, team=match.home_team, payload=payload
                )
            except TrackerCommandError as error:
                return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (first, second)))
    assert Shot.objects.filter(match_data=scoring.tracker.match_data).count() == 1
    assert (
        TrackerCommand.objects.filter(match_data=scoring.tracker.match_data).count()
        == 1
    )
    if same_command:
        assert results[0] == results[1]
        assert isinstance(results[0], dict)
    else:
        assert sum(isinstance(result, dict) for result in results) == 1
        assert "revision_conflict" in results
