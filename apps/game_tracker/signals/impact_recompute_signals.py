"""Coalesce timeline changes into one durable statistics computation."""

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.game_tracker.composition import schedule_match_impact_recompute
from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
    Shot,
)
from apps.game_tracker.services.live_update_signal_control import (
    tracker_delete_side_effects_suppressed,
)


@receiver(post_save, sender=MatchData)
def _match_data_post_save(
    sender: type[MatchData],
    instance: MatchData,
    update_fields: frozenset[str] | None = None,
    **kwargs: object,
) -> None:
    """Include match duration/status changes, but not revision-only publication."""
    if update_fields and set(update_fields) <= {"live_revision", "live_changed_at"}:
        return
    schedule_match_impact_recompute(match_data_id=str(instance.pk), countdown_seconds=2)


@receiver([post_save, post_delete], sender=Shot)
@receiver([post_save, post_delete], sender=PlayerChange)
@receiver([post_save, post_delete], sender=Pause)
@receiver([post_save, post_delete], sender=PlayerGroup)
@receiver([post_save, post_delete], sender=MatchPart)
def _timeline_changed(
    sender: type,
    instance: Shot | PlayerChange | Pause | PlayerGroup | MatchPart,
    **kwargs: object,
) -> None:
    """One pending generation per match, even when a command writes many rows."""
    if tracker_delete_side_effects_suppressed():
        return
    match_data_id = getattr(instance, "match_data_id", None)
    if match_data_id:
        schedule_match_impact_recompute(
            match_data_id=str(match_data_id), countdown_seconds=2
        )


@receiver(m2m_changed, sender=PlayerGroup.players.through)
def _player_group_players_changed(
    sender: type, instance: PlayerGroup, action: str, **kwargs: object
) -> None:
    """Membership edits affect both minutes and impacts."""
    if action in {"post_add", "post_remove", "post_clear"}:
        _timeline_changed(sender, instance)
