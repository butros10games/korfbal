"""Privacy/visibility helpers for player data."""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from apps.team.models.team_data import TeamData

from .models.player import Player
from .models.player_club_membership import PlayerClubMembership


def _prefetched_active_membership_club_ids(player: Player) -> set[str] | None:
    memberships = getattr(player, "_api_active_memberships", None)
    if not isinstance(memberships, list):
        return None
    return {str(membership.club_id) for membership in memberships}


def _prefetched_legacy_club_ids(player: Player) -> set[str] | None:
    playing = getattr(player, "_api_playing_team_data", None)
    coaching = getattr(player, "_api_coaching_team_data", None)
    if not isinstance(playing, list) or not isinstance(coaching, list):
        return None
    return {str(team_data.team.club_id) for team_data in [*playing, *coaching]}


def _active_membership_club_ids(player: Player) -> set[str]:
    """Return active club IDs for the player based on PlayerClubMembership."""
    prefetched = _prefetched_active_membership_club_ids(player)
    if prefetched is not None:
        return prefetched

    today = timezone.localdate()
    return {
        str(club_id)
        for club_id in (
            PlayerClubMembership.objects
            .filter(
                player=player,
                start_date__lte=today,
            )
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .values_list("club_id", flat=True)
            .distinct()
        )
    }


def viewer_connected_to_player_club(*, viewer: Player, target: Player) -> bool:
    """Return True if viewer is connected to at least one club of the target.

    Connection definition (practical, minimal):
    - Prefer explicit active club memberships (PlayerClubMembership).
    - Fall back to legacy inference: both players appear as roster players or
      coaches in any TeamData rows that belong to the same club.
    """
    viewer_membership_ids = _active_membership_club_ids(viewer)
    target_membership_ids = _active_membership_club_ids(target)

    # If either player has explicit memberships configured, use them.
    if viewer_membership_ids or target_membership_ids:
        return bool(viewer_membership_ids & target_membership_ids)

    viewer_prefetched_clubs = _prefetched_legacy_club_ids(viewer)
    target_prefetched_clubs = _prefetched_legacy_club_ids(target)
    if viewer_prefetched_clubs is not None and target_prefetched_clubs is not None:
        return bool(viewer_prefetched_clubs & target_prefetched_clubs)

    viewer_clubs = TeamData.objects.filter(
        Q(players=viewer) | Q(coach=viewer)
    ).values_list("team__club_id", flat=True)

    return TeamData.objects.filter(
        Q(players=target) | Q(coach=target),
        team__club_id__in=viewer_clubs,
    ).exists()


def can_view_by_visibility(
    *,
    visibility: str,
    viewer: Player | None,
    target: Player,
) -> bool:
    """Return True when viewer may see a resource with given visibility."""
    # Owner can always view.
    if viewer is not None and viewer.id_uuid == target.id_uuid:
        return True

    if visibility == Player.Visibility.PUBLIC:
        return True

    # 'private' is deprecated for this app: coaches and club members must still
    # be able to view club-restricted data. Treat it as 'club' for backwards
    # compatibility with older stored values/clients.
    if visibility == Player.Visibility.PRIVATE:
        visibility = Player.Visibility.CLUB

    # Club-restricted: require a connected viewer.
    if visibility == Player.Visibility.CLUB:
        if viewer is None:
            return False
        return viewer_connected_to_player_club(viewer=viewer, target=target)

    # Unknown/legacy values: fail closed.
    return False
