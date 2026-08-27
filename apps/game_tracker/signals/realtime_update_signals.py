"""Advance live revisions for ORM writes outside tracker commands."""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.game_tracker.composition import record_match_change
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
    suppress_live_update_signals,
    tracker_delete_side_effects_suppressed,
)
from apps.game_tracker.services.match_event_context import match_data_is_deleting
from apps.game_tracker.services.match_events import logical_event_id
from apps.game_tracker.services.match_scores import persist_matchdata_scores


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


def _record(
    instance: RealtimeModel,
    resources: set[LiveResource],
    *,
    changed_ids: dict[LiveResource, set[str]] | None = None,
) -> None:
    if live_update_signals_suppressed() or tracker_delete_side_effects_suppressed():
        return
    match_data = instance if isinstance(instance, MatchData) else instance.match_data
    if match_data is not None and not match_data_is_deleting(match_data.pk):
        record_match_change(
            match_data,
            resources=resources,
            changed_ids=changed_ids,
        )


@receiver([post_save, post_delete], sender=Shot)
def _shot_realtime_changed(
    sender: type[Shot], instance: Shot, **kwargs: object
) -> None:
    del sender, kwargs
    if live_update_signals_suppressed() or tracker_delete_side_effects_suppressed():
        return

    match_data = (
        MatchData.objects.filter(id_uuid=instance.match_data_id).first()
        if instance.match_data_id
        else None
    )
    if match_data is not None and match_data.status == "finished":
        # Event-editor corrections must also update the denormalized final score.
        # Coalesce the MatchData save into the Shot's single live revision.
        with suppress_live_update_signals():
            persist_matchdata_scores(match_data)
    entity_id = (
        logical_event_id(
            match_data,
            source_type="shot",
            source_id=instance.pk,
        )
        if match_data is not None
        else str(instance.pk)
    )
    _record(
        instance,
        SHOT_RESOURCES,
        changed_ids={
            LiveResource.EVENTS: {entity_id},
            LiveResource.SHOTS: {entity_id},
        },
    )


@receiver([post_save, post_delete], sender=PlayerChange)
def _substitution_realtime_changed(
    sender: type[PlayerChange],
    instance: PlayerChange,
    **kwargs: object,
) -> None:
    del sender, kwargs
    match_data = instance.match_data
    entity_id = logical_event_id(
        match_data,
        source_type="player_change",
        source_id=instance.pk,
    )
    _record(
        instance,
        SUBSTITUTE_RESOURCES,
        changed_ids={LiveResource.EVENTS: {entity_id}},
    )


@receiver([post_save, post_delete], sender=Pause)
def _pause_realtime_changed(
    sender: type[Pause], instance: Pause, **kwargs: object
) -> None:
    del sender, kwargs
    entity_id = logical_event_id(
        instance.match_data,
        source_type="pause",
        source_id=instance.pk,
    )
    _record(
        instance,
        PAUSE_RESOURCES,
        changed_ids={LiveResource.EVENTS: {entity_id}},
    )


@receiver([post_save, post_delete], sender=Attack)
def _attack_realtime_changed(
    sender: type[Attack],
    instance: Attack,
    **kwargs: object,
) -> None:
    del sender, kwargs
    _record(
        instance,
        {LiveResource.TRACKER, LiveResource.EVENTS},
        changed_ids={LiveResource.EVENTS: set()},
    )


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
