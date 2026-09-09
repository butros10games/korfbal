"""Queries shared by tracker state rendering and undo commands."""

from __future__ import annotations

from typing import cast

from apps.game_tracker.models import (
    Attack,
    MatchData,
    MatchEvent,
    MatchPart,
    Pause,
    PlayerChange,
    PossessionChange,
    Shot,
)
from apps.game_tracker.services.match_events import active_match_events


UndoableMatchEvent = Shot | PlayerChange | PossessionChange | Pause | Attack | MatchPart


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
                "match_part",
            },
        )
        .order_by("-sequence")
        .values_list("source_type", "source_id", "sequence", "kind")
    )
    # Reopening a part corrects its end; it does not create a new start.
    # Keep that original start behind any play already recorded in the part.
    start_sequences = dict(
        MatchEvent.objects.filter(
            match_data=match_data, kind="match_part.started"
        ).values_list("source_id", "sequence")
    )
    ordered = sorted(
        events,
        key=lambda row: (
            start_sequences.get(row[1], row[2])
            if row[0] == "match_part" and row[3] != "match_part.ended"
            else row[2]
        ),
        reverse=True,
    )
    for source_type, source_id, _sequence, _kind in ordered:
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
        elif source_type == "match_part":
            event = MatchPart.objects.filter(
                match_data=match_data, pk=source_id
            ).first()
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
