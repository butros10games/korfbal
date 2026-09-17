"""Compact tracker polls skip configuration that clients already retain."""

from http import HTTPStatus
from typing import Any
from unittest.mock import patch

from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
import pytest

from apps.game_tracker.composition import record_match_change
from apps.game_tracker.models import GoalType, GroupType, MatchData, PlayerGroup
from apps.game_tracker.services.tracker_state import (
    get_tracker_state,
    poll_tracker_state,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_match_part,
    create_tracker_match,
    create_tracker_player,
    login_home_club_editor,
)
from apps.player.models import PlayerSong, PlayerSongStatus
from apps.team.models import TeamData


pytestmark = pytest.mark.django_db
CONFIGURATION_KEYS = {
    "match_id",
    "match_data_id",
    "team",
    "opponent",
    "goal_types",
    "goal_audio",
    "live_revision",
    "last_changed_at",
}
MANIFEST_SERVICE = "apps.game_tracker.services.tracker_state.build_goal_song_manifest"


@pytest.fixture
def tracker() -> TrackerMatchContext:
    """Populate both perspectives with selected player and fallback audio."""
    graph = create_tracker_match(prefix="Compact configuration")
    create_match_part(match_data=graph.match_data)
    kind, _ = GroupType.objects.get_or_create(name="Aanval")
    for index, team in enumerate((graph.home_team, graph.away_team)):
        player = create_tracker_player(username=f"compact-player-{index}")
        group = PlayerGroup.objects.create(
            match_data=graph.match_data,
            team=team,
            starting_type=kind,
            current_type=kind,
        )
        group.players.add(player)
        song = PlayerSong.objects.create(
            player=player,
            status=PlayerSongStatus.READY,
            audio_file=f"synthetic/compact-{index}.mp3",
        )
        player.goal_song_song_ids = [str(song.pk)]
        player.save(update_fields=["goal_song_song_ids"])
        team_data = TeamData.objects.create(
            team=team,
            season=graph.match.season,
            fallback_goal_song_song_ids=[str(song.pk)],
        )
        team_data.players.add(player)
    GoalType.objects.create(name="Compact test goal")
    MatchData.objects.filter(pk=graph.match_data.pk).update(status="active")
    record_match_change(graph.match_data)
    return graph


def _without_server_time(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **state,
        "timer": {
            key: value for key, value in state["timer"].items() if key != "server_time"
        },
    }


@pytest.mark.parametrize("perspective", ["home", "away"])
@pytest.mark.parametrize("since_revision", [-1, 0])
def test_compact_poll_preserves_patch_without_reading_configuration(
    tracker: TrackerMatchContext,
    perspective: str,
    since_revision: int,
) -> None:
    """Every dynamic field matches the full read without rebuilding audio/types."""
    team = tracker.home_team if perspective == "home" else tracker.away_team
    full = get_tracker_state(tracker.match, team=team)
    assert full["goal_audio"]["players"]
    assert full["goal_audio"]["fallback"]
    with (
        patch(MANIFEST_SERVICE) as manifest,
        CaptureQueriesContext(connection) as queries,
    ):
        compact = poll_tracker_state(
            tracker.match,
            team=team,
            since_revision=since_revision,
            compact=True,
        )
    captured = list(queries)
    manifest.assert_not_called()
    assert not any('FROM "game_tracker_goaltype"' in row["sql"] for row in captured)
    assert compact["changed"] is True
    assert compact["live_revision"] == full["live_revision"]
    assert compact["last_changed_at"] == full["last_changed_at"]
    assert compact["resources"]
    expected = {
        key: value for key, value in full.items() if key not in CONFIGURATION_KEYS
    }
    assert _without_server_time(compact["patch"]) == _without_server_time(expected)


@pytest.mark.parametrize("action", ["state", "poll"])
def test_full_tracker_api_retains_audio_and_configuration(
    client: Client,
    tracker: TrackerMatchContext,
    action: str,
) -> None:
    """The default HTTP contract still includes ready player/fallback clips."""
    login_home_club_editor(client, tracker, f"full-{action}-reader")
    response = client.get(
        f"/api/matches/{tracker.match.pk}/tracker/{tracker.home_team.pk}/{action}/",
        {"since_revision": 0},
    )
    assert response.status_code == HTTPStatus.OK
    state = response.json()
    assert state.keys() >= CONFIGURATION_KEYS
    assert state["team"]["id"] == str(tracker.home_team.pk)
    assert state["opponent"]["id"] == str(tracker.away_team.pk)
    assert state["goal_audio"]["players"]
    assert state["goal_audio"]["fallback"]
    assert any(goal["name"] == "Compact test goal" for goal in state["goal_types"])


def test_compact_tracker_api_skips_configuration(
    client: Client,
    tracker: TrackerMatchContext,
) -> None:
    """The opt-in query parameter selects the cheaper read after authorization."""
    login_home_club_editor(client, tracker, "compact-api-reader")
    with patch(MANIFEST_SERVICE) as manifest:
        response = client.get(
            f"/api/matches/{tracker.match.pk}/tracker/{tracker.home_team.pk}/poll/",
            {"since_revision": 0, "compact": "1"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["patch"]["status"] == "active"
    assert not CONFIGURATION_KEYS.intersection(response.json()["patch"])
    manifest.assert_not_called()


@pytest.mark.parametrize("compact", [False, True])
def test_idle_tracker_poll_does_not_build_a_snapshot(
    tracker: TrackerMatchContext,
    compact: bool,
) -> None:
    """Unchanged revisions remain cheap for both client formats."""
    tracker.match_data.refresh_from_db()
    with patch("apps.game_tracker.services.tracker_state.get_tracker_state") as build:
        result = poll_tracker_state(
            tracker.match,
            team=tracker.home_team,
            since_revision=tracker.match_data.live_revision,
            compact=compact,
        )
    assert result["changed"] is False
    assert result["live_revision"] == tracker.match_data.live_revision
    assert "patch" not in result
    build.assert_not_called()
