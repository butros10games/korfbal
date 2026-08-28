"""Optimized query services for player API read models."""

from __future__ import annotations

from django.db import models
from django.db.models import Prefetch, Q, QuerySet
from django.utils import timezone

from apps.player.models.player import Player
from apps.player.models.player_club_membership import PlayerClubMembership
from apps.player.services.player_song_queries import player_song_queryset
from apps.team.models.team_data import TeamData


def _active_memberships_queryset() -> QuerySet[PlayerClubMembership]:
    today = timezone.localdate()
    return (
        PlayerClubMembership.objects
        .select_related("club")
        .filter(start_date__lte=today)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .order_by("club__name", "id_uuid")
        .fetch_mode(models.FETCH_RAISE)
    )


def _team_affiliations_queryset() -> QuerySet[TeamData]:
    return TeamData.objects.select_related("team__club").fetch_mode(models.FETCH_RAISE)


def player_access_queryset() -> QuerySet[Player]:
    """Return players with all relations required by privacy policies."""
    return (
        Player.objects
        .select_related("user")
        .prefetch_related(
            Prefetch(
                "club_membership_links",
                queryset=_active_memberships_queryset(),
                to_attr="_api_active_memberships",
            ),
            Prefetch(
                "team_data_as_player",
                queryset=_team_affiliations_queryset(),
                to_attr="_api_playing_team_data",
            ),
            Prefetch(
                "team_data_as_coach",
                queryset=_team_affiliations_queryset(),
                to_attr="_api_coaching_team_data",
            ),
        )
        .fetch_mode(models.FETCH_RAISE)
    )


def player_detail_queryset() -> QuerySet[Player]:
    """Return players with every relation required by PlayerSerializer."""
    songs = player_song_queryset().order_by("-created_at", "id_uuid")
    return (
        player_access_queryset()
        .prefetch_related(
            "team_follow",
            "club_follow",
            "member_clubs",
            Prefetch(
                "songs",
                queryset=songs,
                to_attr="_api_songs",
            ),
        )
        .fetch_mode(models.FETCH_RAISE)
    )


def player_by_id(player_id: str) -> Player | None:
    """Return one API-ready player by public id."""
    return player_detail_queryset().filter(id_uuid=player_id).first()


def player_for_user_id(user_id: int) -> Player | None:
    """Return one API-ready player by owning user id."""
    return player_detail_queryset().filter(user_id=user_id).first()


def viewer_player_for_user_id(user_id: int) -> Player | None:
    """Return the lightweight Player identity needed by privacy policies."""
    return player_access_queryset().filter(user_id=user_id).first()
