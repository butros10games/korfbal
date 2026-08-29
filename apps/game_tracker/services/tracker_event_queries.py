"""Queries shared by tracker state rendering and undo commands."""

from __future__ import annotations

from typing import cast

from apps.game_tracker.models import (
    Attack,
    MatchData,
    Pause,
    PlayerChange,
    PossessionChange,
    Shot,
)
from apps.game_tracker.services.match_events import active_match_events


UndoableMatchEvent = Shot | PlayerChange | PossessionChange | Pause | Attack


def last_event_model(match_data: MatchData) -> UndoableMatchEvent | None:
    """Resolve the newest undoable fact from its committed event order."""
    events = (
        active_match_events(
            match_data,
            source_types={
                "shot",
                "player_change",
                "possession_change",
                "pause",
                "attack",
            },
        )
        .order_by("-sequence")
        .values_list("source_type", "source_id")
    )
    for source_type, source_id in events:
        event: UndoableMatchEvent | None
        if source_type == "shot":
            event = (
                Shot.objects
                .select_related(
                    "player",
                    "player__user",
                    "shot_type",
                    "match_part",
                    "team",
                )
                .filter(match_data=match_data, pk=source_id)
                .first()
            )
        elif source_type == "player_change":
            event = (
                PlayerChange.objects
                .select_related(
                    "player_in",
                    "player_in__user",
                    "player_out",
                    "player_out__user",
                    "player_group",
                    "match_part",
                )
                .filter(match_data=match_data, pk=source_id)
                .first()
            )
        elif source_type == "pause":
            event = (
                Pause.objects
                .select_related("match_part")
                .filter(match_data=match_data, pk=source_id)
                .first()
            )
        elif source_type == "possession_change":
            event = (
                PossessionChange.objects
                .select_related("player", "player__user", "team", "match_part")
                .filter(match_data=match_data, pk=source_id)
                .first()
            )
        else:
            event = cast(
                Attack | None,
                Attack.objects
                .select_related("team")
                .filter(match_data=match_data, pk=source_id)
                .first(),
            )
        if event is not None:
            return event
    return None
