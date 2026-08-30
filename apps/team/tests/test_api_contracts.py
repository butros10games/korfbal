"""Focused contract and error-path tests for the team API."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Protocol
import uuid

from django.contrib.auth.models import User
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.player.models.player_song import PlayerSongStatus
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData

from .team_test_support import build_team_context, create_player, create_song


pytestmark = pytest.mark.django_db
CATALOG_SIZE = 205
MAX_PAGE_SIZE = 200


class _Settings(Protocol):
    """Settings surface changed by these request tests."""

    SECURE_SSL_REDIRECT: bool


@pytest.fixture(autouse=True)
def _disable_ssl_redirect(settings: _Settings) -> None:
    settings.SECURE_SSL_REDIRECT = False


def test_team_catalog_caps_page_size_and_serializes_nested_club(
    client: Client,
) -> None:
    """The public catalog stays bounded and exposes its read-only club shape."""
    club = Club.objects.create(name="Catalog Club")
    Team.objects.bulk_create([
        Team(name=f"Team {index:03}", club=club) for index in range(CATALOG_SIZE)
    ])

    response = client.get("/api/team/teams/", {"page_size": 999})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["count"] == CATALOG_SIZE
    assert len(payload["results"]) == MAX_PAGE_SIZE
    assert payload["next"] is not None
    club_payload = payload["results"][0]["club"]
    assert club_payload == {
        "id_uuid": str(club.id_uuid),
        "name": club.name,
        "logo": None,
        "logo_url": club_payload["logo_url"],
    }
    assert club_payload["logo_url"].endswith("/images/clubs/blank-club-picture.png")
    assert "club_id" not in payload["results"][0]


def test_anonymous_users_can_read_but_cannot_create_teams(client: Client) -> None:
    """Public reads must not imply anonymous write access."""
    club = Club.objects.create(name="Public Club")
    team = Team.objects.create(name="Public Team", club=club)

    detail_response = client.get(f"/api/team/teams/{team.id_uuid}/")
    create_response = client.post(
        "/api/team/teams/",
        data={"name": "Injected", "club_id": str(club.id_uuid)},
        content_type="application/json",
    )

    assert detail_response.status_code == HTTPStatus.OK
    assert detail_response.json()["id_uuid"] == str(team.id_uuid)
    assert create_response.status_code == HTTPStatus.UNAUTHORIZED
    assert not Team.objects.filter(name="Injected").exists()


def test_staff_create_rejects_unknown_club_without_writing(client: Client) -> None:
    """The write serializer validates the foreign-key identifier."""
    staff = User.objects.create_user(
        username="team_contract_staff",
        password="pass1234",  # nosec
        is_staff=True,
    )
    client.force_login(staff)

    response = client.post(
        "/api/team/teams/",
        data={"name": "Orphan", "club_id": str(uuid.uuid4())},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "club_id" in response.json()
    assert not Team.objects.filter(name="Orphan").exists()


def test_team_catalog_rejects_malformed_club_filter(client: Client) -> None:
    """Malformed UUID filters return a client error instead of crashing the API."""
    Club.objects.create(name="Malformed Filter Club")

    response = client.get("/api/team/teams/", {"club": "not-a-uuid"})

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_impact_breakdown_rejects_malformed_player_id(client: Client) -> None:
    """Malformed player identifiers are invalid input, not server failures."""
    context = build_team_context(suffix="malformed_player")

    response = client.get(
        f"/api/team/teams/{context.team.id_uuid}/impact-breakdown/",
        {"player": "not-a-uuid"},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_goal_song_admin_honors_season_scoping_and_club_admin_role(
    client: Client,
) -> None:
    """A coach manages only their season while a club admin manages all seasons."""
    context = build_team_context(suffix="roles")
    previous = Season.objects.create(
        name="Previous roles",
        start_date=timezone.localdate() - timedelta(days=400),
        end_date=timezone.localdate() - timedelta(days=40),
    )
    previous_data = TeamData.objects.create(team=context.team, season=previous)
    previous_data.coach.add(context.coach)
    context.team_data.coach.remove(context.coach)

    client.force_login(context.coach.user)
    current_response = client.get(
        f"/api/team/teams/{context.team.id_uuid}/goal-song-admin/",
        {"season": str(context.season.id_uuid)},
    )
    previous_response = client.get(
        f"/api/team/teams/{context.team.id_uuid}/goal-song-admin/",
        {"season": str(previous.id_uuid)},
    )

    assert current_response.status_code == HTTPStatus.FORBIDDEN
    assert previous_response.status_code == HTTPStatus.OK

    context.club.admin.add(context.coach)
    club_admin_response = client.get(
        f"/api/team/teams/{context.team.id_uuid}/goal-song-admin/",
        {"season": str(context.season.id_uuid)},
    )
    assert club_admin_response.status_code == HTTPStatus.OK


def test_fallback_update_rejects_invalid_unready_and_outside_roster_songs(
    client: Client,
) -> None:
    """Fallback playlists accept only ready songs owned by roster players."""
    context = build_team_context(suffix="fallback_validation")
    queued = create_song(
        player=context.player,
        title="Queued",
        status=PlayerSongStatus.QUEUED,
    )
    outsider = create_player(username="fallback_outsider")
    outsider_song = create_song(player=outsider, title="Outsider")
    endpoint = f"/api/team/teams/{context.team.id_uuid}/goal-song-admin/fallback/"
    client.force_login(context.coach.user)

    invalid_shape = client.patch(
        endpoint,
        data={"fallback_goal_song_song_ids": "not-a-list"},
        content_type="application/json",
    )
    unknown = client.patch(
        endpoint,
        data={"fallback_goal_song_song_ids": [str(uuid.uuid4())]},
        content_type="application/json",
    )
    unready = client.patch(
        endpoint,
        data={"fallback_goal_song_song_ids": [str(queued.id_uuid)]},
        content_type="application/json",
    )
    outside_roster = client.patch(
        endpoint,
        data={"fallback_goal_song_song_ids": [str(outsider_song.id_uuid)]},
        content_type="application/json",
    )

    assert invalid_shape.status_code == HTTPStatus.BAD_REQUEST
    assert unknown.status_code == HTTPStatus.BAD_REQUEST
    assert unknown.json()["detail"] == "Unknown song id(s)"
    assert unready.status_code == HTTPStatus.BAD_REQUEST
    assert unready.json()["detail"] == "Song(s) not ready"
    assert outside_roster.status_code == HTTPStatus.BAD_REQUEST
    context.team_data.refresh_from_db()
    assert context.team_data.fallback_goal_song_song_ids == []


def test_player_selection_normalizes_ids_and_clears_legacy_fields(
    client: Client,
) -> None:
    """Selection ordering is stable and clearing removes legacy playback state."""
    context = build_team_context(suffix="selection")
    song = create_song(player=context.player, title="Selected")
    endpoint = (
        f"/api/team/teams/{context.team.id_uuid}/goal-song-admin/"
        f"player/{context.player.id_uuid}/"
    )
    client.force_login(context.coach.user)

    selected = client.patch(
        endpoint,
        data={
            "goal_song_song_ids": [
                " ",
                f" {song.id_uuid} ",
                str(song.id_uuid),
            ]
        },
        content_type="application/json",
    )
    cleared = client.patch(
        endpoint,
        data={"goal_song_song_ids": []},
        content_type="application/json",
    )

    assert selected.status_code == HTTPStatus.OK
    assert selected.json()["goal_song_song_ids"] == [str(song.id_uuid)]
    assert cleared.status_code == HTTPStatus.OK
    context.player.refresh_from_db()
    assert context.player.goal_song_song_ids == []
    assert not context.player.goal_song_uri
    assert context.player.song_start_time is None


def test_moderation_rejects_non_roster_players_and_foreign_songs(
    client: Client,
) -> None:
    """Moderators cannot use a team endpoint to mutate unrelated players or songs."""
    context = build_team_context(suffix="ownership")
    outsider = create_player(username="song_ownership_outsider")
    outsider_song = create_song(player=outsider, title="Foreign")
    client.force_login(context.coach.user)

    non_roster_response = client.patch(
        (
            f"/api/team/teams/{context.team.id_uuid}/goal-song-admin/"
            f"player/{outsider.id_uuid}/"
        ),
        data={"goal_song_song_ids": []},
        content_type="application/json",
    )
    foreign_song_response = client.patch(
        (
            f"/api/team/teams/{context.team.id_uuid}/goal-song-admin/"
            f"player/{context.player.id_uuid}/songs/{outsider_song.id_uuid}/settings/"
        ),
        data={"start_time_seconds": 5},
        content_type="application/json",
    )
    missing_delete_response = client.delete(
        f"/api/team/teams/{context.team.id_uuid}/goal-song-admin/"
        f"player/{context.player.id_uuid}/songs/{uuid.uuid4()}/"
    )

    assert non_roster_response.status_code == HTTPStatus.BAD_REQUEST
    assert foreign_song_response.status_code == HTTPStatus.NOT_FOUND
    assert missing_delete_response.status_code == HTTPStatus.NOT_FOUND
    outsider_song.refresh_from_db()


def test_fallback_update_requires_team_data_for_selected_season(
    client: Client,
) -> None:
    """A match-only team season cannot silently create roster configuration."""
    today = timezone.localdate()
    season = Season.objects.create(
        name="Match-only season",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )
    club = Club.objects.create(name="Match-only Club")
    opponent_club = Club.objects.create(name="Match-only Opponent Club")
    team = Team.objects.create(name="Match-only Team", club=club)
    opponent = Team.objects.create(name="Opponent", club=opponent_club)
    Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now() + timedelta(days=1),
    )
    staff = User.objects.create_user(
        username="match_only_staff",
        password="pass1234",  # nosec
        is_staff=True,
    )
    client.force_login(staff)

    response = client.patch(
        f"/api/team/teams/{team.id_uuid}/goal-song-admin/fallback/",
        data={"fallback_goal_song_song_ids": []},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "No TeamData found for this season."
    assert not TeamData.objects.filter(team=team, season=season).exists()
