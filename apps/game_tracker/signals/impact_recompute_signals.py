"""Signals that schedule impact recomputation when match timeline data changes."""

from __future__ import annotations

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.game_tracker.models import Pause, PlayerChange, PlayerGroup, Shot
from apps.game_tracker.services.match_impact_recompute import (
    schedule_match_impact_recompute,
)


def _match_data_id_from_instance(
    instance: Shot | PlayerChange | Pause | PlayerGroup,
) -> str | None:
    match_data_id = getattr(instance, "match_data_id", None)
    if match_data_id:
        return str(match_data_id)

    # Some models connect through a player group.
    player_group_id = getattr(instance, "player_group_id", None)
    if player_group_id:
        group = (
            PlayerGroup.objects.filter(id_uuid=player_group_id)
            .only("match_data_id")
            .first()
        )
        if group and group.match_data_id:
            return str(group.match_data_id)

    return None


@receiver(post_save, sender=Shot)
@receiver(post_delete, sender=Shot)
def _shot_changed(sender: type[Shot], instance: Shot, **kwargs: object) -> None:
    match_data_id = _match_data_id_from_instance(instance)
    if match_data_id:
        schedule_match_impact_recompute(match_data_id=match_data_id)


@receiver(post_save, sender=PlayerChange)
@receiver(post_delete, sender=PlayerChange)
def _player_change_changed(
    sender: type[PlayerChange], instance: PlayerChange, **kwargs: object
) -> None:
    match_data_id = _match_data_id_from_instance(instance)
    if match_data_id:
        schedule_match_impact_recompute(match_data_id=match_data_id)


@receiver(post_save, sender=Pause)
@receiver(post_delete, sender=Pause)
def _pause_changed(sender: type[Pause], instance: Pause, **kwargs: object) -> None:
    match_data_id = _match_data_id_from_instance(instance)
    if match_data_id:
        schedule_match_impact_recompute(match_data_id=match_data_id)


@receiver(post_save, sender=PlayerGroup)
@receiver(post_delete, sender=PlayerGroup)
def _player_group_changed(
    sender: type[PlayerGroup], instance: PlayerGroup, **kwargs: object
) -> None:
    match_data_id = _match_data_id_from_instance(instance)
    if match_data_id:
        schedule_match_impact_recompute(match_data_id=match_data_id)


@receiver(m2m_changed, sender=PlayerGroup.players.through)
def _player_group_players_changed(
    sender: type[object], instance: PlayerGroup, action: str, **kwargs: object
) -> None:
    if action not in {"post_add", "post_remove", "post_clear"}:
        return

    match_data_id = _match_data_id_from_instance(instance)
    if match_data_id:
        schedule_match_impact_recompute(match_data_id=match_data_id)
