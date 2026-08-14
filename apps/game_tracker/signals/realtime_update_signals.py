"""Advance live revisions for ORM writes outside tracker commands."""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.game_tracker.models import (
    Attack,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
)
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource
from apps.game_tracker.services.live_update_signal_control import (
    live_update_signals_suppressed,
)
from apps.game_tracker.services.live_updates import record_match_change


RealtimeModel = MatchData | Shot | PlayerChange | Pause | Attack | MatchPart

SHOT_RESOURCES = {
    LiveResource.LIVE,
    LiveResource.TRACKER,
    LiveResource.SUMMARY,
    LiveResource.EVENTS,
    LiveResource.SHOTS,
    LiveResource.STATS,
    LiveResource.IMPACTS,
    LiveResource.MVP,
}
SUBSTITUTE_RESOURCES = {
    LiveResource.TRACKER,
    LiveResource.EVENTS,
    LiveResource.PLAYER_GROUPS,
    LiveResource.STATS,
    LiveResource.IMPACTS,
}
PAUSE_RESOURCES = {LiveResource.LIVE, LiveResource.TRACKER, LiveResource.EVENTS}


def _record(instance: RealtimeModel, resources: set[LiveResource]) -> None:
    if live_update_signals_suppressed():
        return
    match_data = instance if isinstance(instance, MatchData) else instance.match_data
    if match_data is not None:
        record_match_change(match_data, resources=resources)


@receiver([post_save, post_delete], sender=Shot)
def _shot_realtime_changed(
    sender: type[Shot], instance: Shot, **kwargs: object
) -> None:
    del sender, kwargs
    _record(instance, SHOT_RESOURCES)


@receiver([post_save, post_delete], sender=PlayerChange)
def _substitution_realtime_changed(
    sender: type[PlayerChange],
    instance: PlayerChange,
    **kwargs: object,
) -> None:
    del sender, kwargs
    _record(instance, SUBSTITUTE_RESOURCES)


@receiver([post_save, post_delete], sender=Pause)
def _pause_realtime_changed(
    sender: type[Pause], instance: Pause, **kwargs: object
) -> None:
    del sender, kwargs
    _record(instance, PAUSE_RESOURCES)


@receiver([post_save, post_delete], sender=Attack)
def _attack_realtime_changed(
    sender: type[Attack],
    instance: Attack,
    **kwargs: object,
) -> None:
    del sender, kwargs
    _record(instance, {LiveResource.TRACKER, LiveResource.EVENTS})


@receiver([post_save, post_delete], sender=MatchPart)
def _match_part_realtime_changed(
    sender: type[MatchPart],
    instance: MatchPart,
    **kwargs: object,
) -> None:
    del sender, kwargs
    _record(instance, set(ALL_LIVE_RESOURCES))


@receiver(post_save, sender=MatchData)
def _match_data_realtime_changed(
    sender: type[MatchData],
    instance: MatchData,
    created: bool,
    update_fields: frozenset[str] | None,
    **kwargs: object,
) -> None:
    del sender, kwargs
    if created or (
        update_fields and update_fields <= {"live_revision", "live_changed_at"}
    ):
        return
    _record(instance, set(ALL_LIVE_RESOURCES))
