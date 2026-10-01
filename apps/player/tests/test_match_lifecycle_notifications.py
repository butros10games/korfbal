# ruff: noqa: D103
"""Match pushes reach team followers (not club followers); finishes reach players."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.utils import timezone
import pytest

from apps.game_tracker.models import MatchPlayer
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_group_types,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import Player
from apps.player.services.match_notifications import (
    FinishedMatchJobs,
    handle_finished_match,
    handle_started_match,
)


pytestmark = pytest.mark.django_db


def _audience(tracker: TrackerMatchContext) -> dict[str, Player]:
    """Create team/club followers, grouped and lineup players, and an outsider."""
    players = {
        name: create_tracker_player(username=f"lifecycle-{name}")
        for name in (
            "team_follower",
            "club_follower",
            "grouped_follower",
            "lineup_player",
            "outsider",
        )
    }
    players["team_follower"].team_follow.add(tracker.away_team)
    players["club_follower"].club_follow.add(tracker.home_team.club)
    players["grouped_follower"].team_follow.add(tracker.home_team)
    group_type = create_group_types("Aanval")["Aanval"]
    create_player_group(
        match_data=tracker.match_data, team=tracker.home_team, group_type=group_type
    ).players.add(players["grouped_follower"])
    MatchPlayer.objects.create(
        match_data=tracker.match_data,
        player=players["lineup_player"],
        team=tracker.home_team,
    )
    return players


def _user_ids(players: dict[str, Player], *names: str) -> list[int]:
    return sorted(int(players[name].user.pk) for name in names)


def test_match_start_notifies_followers_who_are_not_playing() -> None:
    tracker = create_tracker_match(prefix="Lifecycle start")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    players = _audience(tracker)
    send_payload = Mock()

    handle_started_match(
        match_id=str(tracker.match.pk),
        match_data_id=str(tracker.match_data.pk),
        send_payload=send_payload,
    )

    send_payload.assert_called_once()
    kwargs = send_payload.call_args.kwargs
    assert kwargs["user_ids"] == _user_ids(players, "team_follower")
    assert kwargs["payload"].title == "Wedstrijd begonnen"
    assert kwargs["payload"].tag == f"match-started:{tracker.match_data.pk}"


def test_undone_match_start_is_not_announced() -> None:
    tracker = create_tracker_match(prefix="Lifecycle undone")
    _audience(tracker)
    send_payload = Mock()

    handle_started_match(
        match_id=str(tracker.match.pk),
        match_data_id=str(tracker.match_data.pk),
        send_payload=send_payload,
    )

    send_payload.assert_not_called()


def test_match_finish_notifies_followers_and_players() -> None:
    tracker = create_tracker_match(prefix="Lifecycle finish")
    tracker.match_data.status = "finished"
    tracker.match_data.home_score = 14
    tracker.match_data.away_score = 12
    tracker.match_data.save(update_fields=["status", "home_score", "away_score"])
    players = _audience(tracker)
    send_payload = Mock()

    with patch(
        "apps.player.services.match_notifications.mvp_service.get_or_create_match_mvp",
        return_value=SimpleNamespace(closes_at=timezone.now() + timedelta(hours=2)),
    ):
        handle_finished_match(
            match_id=str(tracker.match.pk),
            match_data_id=str(tracker.match_data.pk),
            jobs=FinishedMatchJobs(
                send_payload=send_payload,
                schedule_reminder=Mock(),
                schedule_publish=Mock(),
            ),
        )

    kwargs = send_payload.call_args.kwargs
    assert kwargs["user_ids"] == _user_ids(
        players, "team_follower", "grouped_follower", "lineup_player"
    )
    assert kwargs["payload"].body == (
        f"{tracker.home_team.name} 14 - 12 {tracker.away_team.name}. "
        "De statistieken staan klaar."
    )
