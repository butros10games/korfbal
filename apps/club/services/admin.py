"""Club admin workflow services."""

from __future__ import annotations

from datetime import date
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.club.models.club import Club
from apps.player.models.player import Player
from apps.player.models.player_club_membership import PlayerClubMembership


MIN_USER_SEARCH_TERM_LENGTH = 2


def get_club_admin_settings_data(
    *,
    club: Club,
) -> tuple[list[Player], list[PlayerClubMembership]]:
    """Return active club admin and membership records for settings."""
    today = timezone.localdate()
    admins = list(club.admin.select_related("user").order_by("user__username"))
    memberships = list(
        PlayerClubMembership.objects
        .select_related("player", "player__user")
        .filter(club=club, start_date__lte=today)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .order_by("player__user__username")
    )
    return admins, memberships


def search_club_admin_users(*, club: Club, term: str) -> list[dict[str, object]]:
    """Find accounts a club admin may add as members.

    Substring matches are limited to players already connected to this club
    (members past or present, rostered players and coaches, followers). Anyone
    else is only found by their exact username, so a club admin cannot page
    through every account on the platform.
    """
    if len(term) < MIN_USER_SEARCH_TERM_LENGTH:
        return []

    connected = (
        Q(member_clubs=club)
        | Q(team_data_as_player__team__club=club)
        | Q(team_data_as_coach__team__club=club)
        | Q(club_follow=club)
    )
    partial = Q(user__username__icontains=term) | Q(name__icontains=term)
    players = (
        Player.objects
        .filter(user__isnull=False)
        .filter(Q(user__username__iexact=term) | (connected & partial))
        .select_related("user")
        .distinct()
        .order_by("user__username")[:20]
    )
    return [
        {
            "user_id": player.user_id,
            "username": str(player.user.username),
            "player_id": str(player.id_uuid),
        }
        for player in players
    ]


def resolve_player_for_membership(data: dict[str, Any]) -> Player | None:
    """Resolve or create the player targeted by a membership payload."""
    player_id = data.get("player_id")
    if player_id:
        return Player.objects.filter(id_uuid=player_id).select_related("user").first()

    user_model = get_user_model()
    user_id = data.get("user_id")
    username = data.get("username")
    if user_id:
        user = user_model.objects.filter(id=user_id).first()
    elif username:
        user = user_model.objects.filter(username__iexact=username).first()
    else:
        user = None

    if user is None:
        return None

    player, _ = Player.objects.get_or_create(user=user)
    return Player.objects.filter(id_uuid=player.id_uuid).select_related("user").first()


@transaction.atomic
def create_active_membership(
    *,
    club: Club,
    player: Player,
    start_date: date | None = None,
) -> tuple[PlayerClubMembership, bool]:
    """Create an active club membership when one does not already exist."""
    Player.all_objects.select_for_update().get(pk=player.pk)
    existing = PlayerClubMembership.objects.filter(
        player=player, club=club, end_date__isnull=True
    ).first()
    if existing is not None:
        return existing, False
    membership = PlayerClubMembership(
        player=player, club=club, start_date=start_date or timezone.localdate()
    )
    membership.full_clean()
    membership.save()
    return membership, True


def close_active_membership(
    *,
    club: Club,
    player_id: str,
) -> bool:
    """Close the player's active club membership if one exists."""
    membership = (
        PlayerClubMembership.objects
        .filter(
            club=club,
            player_id=player_id,
            end_date__isnull=True,
        )
        .order_by("-start_date")
        .first()
    )
    if membership is None:
        return False

    PlayerClubMembership.objects.filter(pk=membership.pk).update(
        end_date=timezone.localdate(),
    )
    return True
