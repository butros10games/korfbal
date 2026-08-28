"""Shot and goal registration commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import models

from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchEvent,
    MatchPart,
    Shot,
    ShotEventDetail,
)
from apps.game_tracker.services.event_reconciliation import (
    ShotObservation,
    create_reconciliation_candidates,
    plan_shot_reconciliation,
    record_matched_observation,
)
from apps.game_tracker.services.lineup_projections import rebuild_group_roles
from apps.player.models import Player
from apps.team.models.team import Team

from .base import TrackerCommandContext, TrackerCommandError, require_live_part


def _match_player(
    *,
    match_data: MatchData,
    team: Team,
    player_id: str,
) -> Player:
    try:
        player = (
            Player.objects
            .select_related("user")
            .filter(id_uuid=player_id)
            .filter(
                models.Q(
                    player_groups__match_data=match_data,
                    player_groups__team=team,
                )
                | models.Q(
                    match_players__match_data=match_data,
                    match_players__team=team,
                )
            )
            .distinct()
            .first()
        )
    except (ValidationError, ValueError) as exc:
        raise TrackerCommandError("Invalid player.", code="bad_request") from exc
    if player is None:
        raise TrackerCommandError(
            "Player is not registered for this team.", code="bad_request"
        )
    return player


@dataclass(frozen=True, slots=True)
class _ShotRegistration:
    match_data: MatchData
    match_part: MatchPart
    reporting_team: Team
    shooting_team: Team
    player: Player
    for_team: bool
    shot_type: GoalType | None
    outcome: str
    event_time: datetime


def _record_shot_observation(registration: _ShotRegistration) -> bool:
    plan = plan_shot_reconciliation(
        ShotObservation(
            match_data=registration.match_data,
            match_part=registration.match_part,
            reporting_team_id=registration.reporting_team.pk,
            shooting_team_id=registration.shooting_team.pk,
            outcome=registration.outcome,
            shot_type=registration.shot_type,
            effective_at=registration.event_time,
        )
    )
    observation_payload = {
        "kind": "shot",
        "shooting_team_id": str(registration.shooting_team.pk),
        "reporting_team_id": str(registration.reporting_team.pk),
        "reported_player_id": str(registration.player.pk),
        "reported_player_role": "shooter" if registration.for_team else "defender",
        "shot_type_id": (
            str(registration.shot_type.pk) if registration.shot_type else None
        ),
        "outcome": registration.outcome,
    }
    if plan.matched_event is not None:
        record_matched_observation(
            event=plan.matched_event,
            effective_at=registration.event_time,
            payload=observation_payload,
        )
        return False

    shot = Shot.objects.create(
        player=registration.player,
        match_data=registration.match_data,
        match_part=registration.match_part,
        time=registration.event_time,
        for_team=registration.for_team,
        team=registration.shooting_team,
        shot_type=registration.shot_type,
        scored=registration.outcome == ShotEventDetail.OUTCOME_GOAL,
    )
    event = MatchEvent.objects.get(source_type="shot", source_id=shot.pk)
    create_reconciliation_candidates(
        event=event,
        possible_duplicates=plan.review_events,
    )
    return True


@dataclass(frozen=True, slots=True)
class ShotCommand:
    """Register a missed shot for either participating team."""

    player_id: str
    for_team: bool
    shot_type_id: str | None = None

    def apply(self, context: TrackerCommandContext) -> None:
        """Register the shot.

        Raises:
            TrackerCommandError: If play, player, or shot-type state is invalid.

        """
        match_part, opponent = require_live_part(
            context.match_data,
            context.team,
            context.match,
        )
        player = _match_player(
            match_data=context.match_data,
            team=context.team,
            player_id=self.player_id,
        )
        shot_type = None
        if self.shot_type_id:
            try:
                shot_type = GoalType.objects.get(id_uuid=self.shot_type_id)
            except (GoalType.DoesNotExist, ValidationError, ValueError) as exc:
                raise TrackerCommandError(
                    "Invalid shot type.",
                    code="bad_request",
                ) from exc

        _record_shot_observation(
            _ShotRegistration(
                player=player,
                match_data=context.match_data,
                match_part=match_part,
                reporting_team=context.team,
                shooting_team=context.team if self.for_team else opponent,
                for_team=self.for_team,
                shot_type=shot_type,
                outcome=ShotEventDetail.OUTCOME_MISS,
                event_time=context.event_time,
            )
        )


@dataclass(frozen=True, slots=True)
class GoalCommand:
    """Register a scored shot for either participating team."""

    player_id: str
    goal_type_id: str
    for_team: bool

    def apply(self, context: TrackerCommandContext) -> None:
        """Register the goal.

        Raises:
            TrackerCommandError: If play, player, or goal-type state is invalid.

        """
        match_part, opponent = require_live_part(
            context.match_data,
            context.team,
            context.match,
        )
        player = _match_player(
            match_data=context.match_data,
            team=context.team,
            player_id=self.player_id,
        )
        try:
            goal_type = GoalType.objects.get(id_uuid=self.goal_type_id)
        except (GoalType.DoesNotExist, ValidationError, ValueError) as exc:
            raise TrackerCommandError("Invalid goal type.", code="bad_request") from exc

        created = _record_shot_observation(
            _ShotRegistration(
                player=player,
                match_data=context.match_data,
                match_part=match_part,
                reporting_team=context.team,
                shooting_team=context.team if self.for_team else opponent,
                for_team=self.for_team,
                shot_type=goal_type,
                outcome=ShotEventDetail.OUTCOME_GOAL,
                event_time=context.event_time,
            )
        )
        if created:
            rebuild_group_roles(context.match_data)
