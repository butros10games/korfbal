"""Undo the newest committed tracker event."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from apps.game_tracker.models import (
    Attack,
    Pause,
    PlayerChange,
    PossessionChange,
    Shot,
    Timeout,
)
from apps.game_tracker.services.lineup_projections import (
    rebuild_current_lineup,
    rebuild_group_roles,
)
from apps.game_tracker.services.tracker_event_queries import last_event_model

from .base import TrackerCommandContext, other_team


def _remove_shot(event: Shot, *, context: TrackerCommandContext) -> None:
    scored = event.scored
    event.__dict__.setdefault("match_data_id", context.match_data.pk)
    event.delete()
    if scored:
        rebuild_group_roles(context.match_data)


def _remove_player_change(
    event: PlayerChange,
    *,
    context: TrackerCommandContext,
) -> None:
    has_players = event.player_in is not None and event.player_out is not None
    event.delete()
    if has_players:
        rebuild_current_lineup(context.match_data)


def _remove_pause(event: Pause) -> None:
    timeout = Timeout.objects.filter(pause=event).first()
    if timeout is not None:
        timeout.delete()
    if event.active:
        event.delete()
        return

    event.active = True
    cast(Any, event).end_time = None
    event.save(update_fields=["active", "end_time"])


@dataclass(frozen=True, slots=True)
class RemoveLastEventCommand:
    """Remove or reopen the newest undoable tracker event."""

    def apply(self, context: TrackerCommandContext) -> None:
        """Undo the event when one exists."""
        other_team(context.match, context.team)
        event = last_event_model(context.match_data)
        if isinstance(event, Shot):
            _remove_shot(event, context=context)
        elif isinstance(event, PlayerChange):
            _remove_player_change(event, context=context)
        elif isinstance(event, Pause):
            _remove_pause(event)
        elif isinstance(event, PossessionChange | Attack):
            event.delete()
