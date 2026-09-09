"""Undo the newest committed tracker event."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from apps.game_tracker.models import (
    Attack,
    MatchPart,
    Pause,
    PlayerChange,
    PossessionChange,
    Shot,
    StartingPlayerAssignment,
    Timeout,
)
from apps.game_tracker.services.lineup_projections import (
    rebuild_current_lineup,
    rebuild_group_roles,
)
from apps.game_tracker.services.tracker_event_queries import last_event_model

from .base import TrackerCommandContext, TrackerCommandError, other_team


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


@dataclass(frozen=True, slots=True)
class UndoPartTransitionCommand:
    """Undo a selected period boundary without deleting dependent match records."""

    part_id: str
    transition: str

    def apply(self, context: TrackerCommandContext) -> None:
        """Restore the previous lifecycle state under the tracker aggregate lock.

        Raises:
            TrackerCommandError: If the boundary is stale or has dependent events.

        """
        match_data = context.match_data
        part = MatchPart.objects.filter(match_data=match_data, pk=self.part_id).first()
        if part is None:
            raise TrackerCommandError(
                "Periode bestaat niet meer.", code="invalid_transition"
            )
        if MatchPart.objects.filter(
            match_data=match_data, part_number__gt=part.part_number
        ).exists():
            raise TrackerCommandError(
                "Maak eerst de start van de volgende periode ongedaan.",
                code="later_part_exists",
            )
        if self.transition == "end":
            if part.end_time is None or part.active:
                raise TrackerCommandError(
                    "Deze periode is niet beëindigd.", code="invalid_transition"
                )
            ended_at = part.end_time
            part.end_time = None
            part.active = True
            part.save(update_fields=["end_time", "active"])
            # Freeze the clock at the removed boundary. Resuming excludes the
            # time spent between periods, including time spent correcting it.
            pause = (
                Pause.objects
                .filter(match_data=match_data, match_part=part, end_time=ended_at)
                .order_by("-start_time")
                .first()
            )
            if pause is None:
                Pause.objects.create(
                    match_data=match_data,
                    match_part=part,
                    start_time=ended_at,
                    active=True,
                )
            else:
                pause.end_time = None
                pause.active = True
                pause.save(update_fields=["end_time", "active"])
            match_data.status = "active"
        else:
            if part.end_time is not None or not part.active:
                raise TrackerCommandError(
                    "Maak eerst het einde van deze periode ongedaan.",
                    code="invalid_transition",
                )
            for model in (Shot, PlayerChange, PossessionChange, Attack, Pause, Timeout):
                if model.objects.filter(
                    match_data=match_data, match_part=part
                ).exists():
                    raise TrackerCommandError(
                        "Verwijder eerst de events in deze periode.",
                        code="part_has_events",
                    )
            part.delete()
            match_data.status = "upcoming" if part.part_number == 1 else "active"
            if part.part_number == 1:
                StartingPlayerAssignment.objects.filter(match_data=match_data).delete()
        match_data.current_part = part.part_number
        match_data.save(update_fields=["status", "current_part"])
