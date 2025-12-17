"""Privacy/visibility helpers for player data."""

from __future__ import annotations

from django.db.models import Q

from apps.team.models.team_data import TeamData

from .models.player import Player


def viewer_connected_to_player_club(*, viewer: Player, target: Player) -> bool:
    """Return True if viewer is connected to at least one club of the target.

    Connection definition (practical, minimal):
    - both players appear as roster players or coaches in any TeamData
      rows that belong to the same club.
    """
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
