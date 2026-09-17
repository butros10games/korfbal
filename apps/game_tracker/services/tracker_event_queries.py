"""Queries shared by tracker state rendering and undo commands."""

from __future__ import annotations

from typing import cast

from django.db import models
from django.db.models.functions import Coalesce

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
    events = active_match_events(
        match_data,
        source_types={
            "shot",
            "player_change",
            "possession_change",
            "pause",
            "attack",
            "match_part",
        },
    ).order_by("-sequence")
    newest = events.values_list("source_type", "source_id", "sequence", "kind").first()
    if newest is None:
        return None
    source_type, source_id, sequence, kind = newest
    if source_type != "match_part" or kind in {
        "match_part.started",
        "match_part.ended",
    }:
        event = _event_model(match_data, source_type=source_type, source_id=source_id)
        if event is not None:
            return event
        events = events.exclude(sequence=sequence)

    # A reopened part retains its original start's undo position. Keep the
    # ordering in SQL so a normal read need not load and sort the whole history.
    start_sequence = (
        MatchEvent.objects
        .filter(
            match_data=match_data,
            source_id=models.OuterRef("source_id"),
            kind="match_part.started",
        )
        .order_by("-sequence")
        .values("sequence")[:1]
    )
    ordered = (
        events
        .alias(
            undo_sequence=models.Case(
                models.When(
                    models.Q(source_type="match_part")
                    & ~models.Q(kind="match_part.ended"),
                    then=Coalesce(models.Subquery(start_sequence), "sequence"),
                ),
                default="sequence",
            )
        )
        .order_by("-undo_sequence", "-sequence")
        .values_list("source_type", "source_id")
    )
    for source_type, source_id in ordered.iterator(chunk_size=32):
        event = _event_model(match_data, source_type=source_type, source_id=source_id)
        if event is not None:
            return event
    return None


def _event_model(
    match_data: MatchData, *, source_type: str, source_id: object
) -> UndoableMatchEvent | None:
    """Load a typed source with the relations needed by snapshots and undo."""
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
        event = MatchPart.objects.filter(match_data=match_data, pk=source_id).first()
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
    return event
