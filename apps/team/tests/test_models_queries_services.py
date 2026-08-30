"""Model, query, and command invariants for the team app."""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
import pytest

from apps.game_tracker.models import MatchData
from apps.player.models.player_song import PlayerSong
from apps.schedule.models import Season
from apps.team.models import TeamData
from apps.team.queries.overview import main_roster_ids, team_players, team_seasons
from apps.team.services.goal_songs import delete_team_player_song

from .team_test_support import build_team_context, create_player, create_song


pytestmark = pytest.mark.django_db


def test_team_data_defaults_and_human_readable_labels() -> None:
    """New roster configuration has safe defaults and useful admin labels."""
    context = build_team_context(suffix="model_defaults")

    assert str(context.team) == f"{context.club.name} {context.team.name}"
    assert str(context.team_data) == context.team.name
    assert not context.team_data.competition
    assert context.team_data.wedstrijd_sport is False
    assert context.team_data.team_rank == 1
    assert context.team_data.fallback_goal_song_song_ids == []


@override_settings(WEB_APP_ORIGIN="https://web.example")
def test_team_absolute_url_targets_the_spa() -> None:
    """Model links follow the active React team-detail route."""
    context = build_team_context(suffix="absolute_url")

    assert context.team.get_absolute_url() == (
        f"https://web.example/teams/{context.team.id_uuid}"
    )


def test_team_queries_keep_rosters_scoped_to_the_requested_season() -> None:
    """Players and main-roster ids from another season must not leak into results."""
    context = build_team_context(suffix="query_scope")
    previous = Season.objects.create(
        name="Previous query scope",
        start_date=timezone.localdate() - timedelta(days=400),
        end_date=timezone.localdate() - timedelta(days=40),
    )
    previous_player = create_player(username="previous_roster_player")
    previous_data = TeamData.objects.create(team=context.team, season=previous)
    previous_data.players.add(previous_player)

    current_players = list(
        team_players(
            context.team,
            context.season,
            MatchData.objects.none(),
        )
    )

    assert [player.id_uuid for player in current_players] == [context.player.id_uuid]
    assert main_roster_ids(team=context.team, season=context.season) == {
        str(context.player.id_uuid)
    }


def test_team_seasons_are_distinct_and_newest_first() -> None:
    """Multiple roster links do not duplicate season filter options."""
    context = build_team_context(suffix="season_options")
    older = Season.objects.create(
        name="Older season option",
        start_date=timezone.localdate() - timedelta(days=500),
        end_date=timezone.localdate() - timedelta(days=100),
    )
    first = TeamData.objects.create(team=context.team, season=older)
    second = TeamData.objects.create(team=context.team, season=older)
    first.players.add(context.player)
    second.players.add(context.coach)

    assert list(team_seasons(context.team)) == [context.season, older]


def test_delete_team_player_song_updates_player_and_fallback_references() -> None:
    """Deleting a song atomically removes every dangling team/player selection."""
    context = build_team_context(suffix="delete_service")
    deleted = create_song(player=context.player, title="Deleted")
    retained = create_song(player=context.player, title="Retained")
    deleted_id = str(deleted.id_uuid)
    retained_id = str(retained.id_uuid)
    context.player.goal_song_song_ids = [deleted_id, retained_id]
    context.player.save(update_fields=["goal_song_song_ids"])
    context.team_data.fallback_goal_song_song_ids = [
        deleted_id,
        retained_id,
        deleted_id,
    ]
    context.team_data.save(update_fields=["fallback_goal_song_song_ids"])

    delete_team_player_song(
        player=context.player,
        song_id=deleted_id,
        team_data=context.team_data,
    )

    context.player.refresh_from_db()
    context.team_data.refresh_from_db()
    assert context.player.goal_song_song_ids == [retained_id]
    assert context.team_data.fallback_goal_song_song_ids == [retained_id]
    assert not PlayerSong.objects.filter(
        player=context.player,
        id_uuid=deleted.id_uuid,
    ).exists()
    assert PlayerSong.objects.filter(
        player=context.player,
        id_uuid=retained.id_uuid,
    ).exists()


def test_delete_team_player_song_supports_player_only_cleanup() -> None:
    """The command remains valid when no season-specific TeamData exists."""
    context = build_team_context(suffix="delete_without_team_data")
    song = create_song(player=context.player, title="Player only")

    delete_team_player_song(
        player=context.player,
        song_id=str(song.id_uuid),
        team_data=None,
    )

    assert not PlayerSong.objects.filter(
        player=context.player,
        id_uuid=song.id_uuid,
    ).exists()
