"""Possession-change registration commands."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from apps.game_tracker.models import PossessionChange
from apps.player.models import Player

from .base import TrackerCommandContext, TrackerCommandError, require_live_part


_REQUIRED_ROLE_BY_KIND = {
    PossessionChange.BALL_LOSS: "Aanval",
    PossessionChange.INTERCEPTION: "Verdediging",
}


def _active_player_for_kind(
    *,
    context: TrackerCommandContext,
    player_id: str,
    kind: str,
) -> Player:
    required_role = _REQUIRED_ROLE_BY_KIND[kind]
    try:
        player = (
            Player.objects
            .select_related("user")
            .filter(id_uuid=player_id)
            .filter(
                player_groups__match_data=context.match_data,
                player_groups__team=context.team,
                player_groups__current_type__name=required_role,
            )
            .distinct()
            .first()
        )
    except (ValidationError, ValueError) as exc:
        raise TrackerCommandError("Invalid player.", code="bad_request") from exc
    if player is None:
        label = "attacking" if kind == PossessionChange.BALL_LOSS else "defending"
        raise TrackerCommandError(
            f"Player is not an active {label} player for this team.",
            code="bad_request",
        )
    return player


@dataclass(frozen=True, slots=True)
class PossessionChangeCommand:
    """Register a ball loss or interception for the reporting team."""

    player_id: str | None
    kind: str

    def apply(self, context: TrackerCommandContext) -> None:
        """Register the possession change for an active on-court player."""
        match_part, _opponent = require_live_part(
            context.match_data,
            context.team,
            context.match,
        )
        player = (
            _active_player_for_kind(
                context=context,
                player_id=self.player_id,
                kind=self.kind,
            )
            if self.player_id is not None
            else None
        )
        PossessionChange.objects.create(
            match_data=context.match_data,
            match_part=match_part,
            team=context.team,
            player=player,
            kind=self.kind,
            time=context.event_time,
        )
