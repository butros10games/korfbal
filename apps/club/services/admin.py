"""Club admin workflow services."""

from __future__ import annotations

from datetime import date
from typing import Any

from django.contrib.auth import get_user_model
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


def search_club_admin_users(*, term: str) -> list[dict[str, object]]:
    """Search users/players that can be added to club membership."""
    if len(term) < MIN_USER_SEARCH_TERM_LENGTH:
        return []

    user_model = get_user_model()
    users = (
        user_model.objects
        .filter(username__icontains=term)
        .order_by("username")
        .only("id", "username")[:20]
    )

    players_by_user_id = {
        player.user_id: player
        for player in Player.objects.filter(user__in=users).select_related("user")
    }

    results: list[dict[str, object]] = []
    for user in users:
        user_id = getattr(user, "id", None)
        username = str(getattr(user, "username", ""))
        player = players_by_user_id.get(user_id) if user_id is not None else None
        results.append(
            {
                "user_id": user_id,
                "username": username,
                "player_id": str(player.id_uuid) if player else None,
            },
        )
    return results


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


def create_active_membership(
    *,
    club: Club,
    player: Player,
    start_date: date | None = None,
) -> tuple[PlayerClubMembership, bool]:
    """Create an active club membership when one does not already exist."""
    return PlayerClubMembership.objects.get_or_create(
        player=player,
        club=club,
        end_date__isnull=True,
        defaults={"start_date": start_date or timezone.localdate()},
    )


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
