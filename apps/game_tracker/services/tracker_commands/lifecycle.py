"""Match lifecycle, clock, timeout, and attack commands."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from uuid import UUID

from django.db import transaction

from apps.game_tracker.application.ports import TrackerJobDispatcher
from apps.game_tracker.domain.match_limits import MAX_TIMEOUTS_PER_TEAM
from apps.game_tracker.models import (
    Attack,
    MatchData,
    MatchEvent,
    MatchPart,
    Pause,
    Timeout,
)
from apps.game_tracker.services.event_reconciliation import (
    SimpleEventObservation,
    create_reconciliation_candidates,
    plan_simple_event_reconciliation,
    record_matched_observation,
)
from apps.game_tracker.services.lineup_projections import capture_starting_lineup
from apps.game_tracker.services.match_scores import compute_scores_for_matchdata_ids
from apps.schedule.models import Match

from .base import (
    NO_ACTIVE_MATCH_PART_MESSAGE,
    TrackerCommandContext,
    TrackerCommandError,
    current_part,
    other_team,
    require_live_part,
)


logger = logging.getLogger(__name__)


def _prepare_new_part(match_data: MatchData) -> None:
    if match_data.status == "finished":
        raise TrackerCommandError(
            "Finished matches cannot be restarted.",
            code="match_finished",
        )

    if match_data.status == "upcoming":
        if (
            match_data.current_part != 1
            or MatchPart.objects.filter(match_data=match_data).exists()
        ):
            raise TrackerCommandError(
                "Match has an invalid initial state.",
                code="invalid_match_state",
            )
        match_data.status = "active"
        match_data.save(update_fields=["status"])
        return

    if match_data.status != "active":
        raise TrackerCommandError(
            "Match cannot be started from its current state.",
            code="invalid_match_state",
        )

    previous_part_number = match_data.current_part - 1
    previous_part_finished = MatchPart.objects.filter(
        match_data=match_data,
        part_number=previous_part_number,
        active=False,
        end_time__isnull=False,
    ).exists()
    if previous_part_number < 1 or not previous_part_finished:
        raise TrackerCommandError(
            "The previous match part has not been completed.",
            code="invalid_match_state",
        )
    if MatchPart.objects.filter(
        match_data=match_data,
        part_number=match_data.current_part,
    ).exists():
        raise TrackerCommandError(
            "This match part has already been started.",
            code="invalid_match_state",
        )


def _timer_report_matches(observation: SimpleEventObservation) -> bool:
    plan = plan_simple_event_reconciliation(observation)
    if plan.matched_event is None:
        return False
    record_matched_observation(
        event=plan.matched_event,
        effective_at=observation.effective_at,
        payload={
            "kind": observation.source_type,
            "reporting_team_id": str(observation.reporting_team_id),
            "record": observation.record_filters,
        },
    )
    return True


@dataclass(frozen=True, slots=True)
class StartPauseCommand:
    """Start a match part or toggle its active pause."""

    def apply(self, context: TrackerCommandContext) -> None:
        """Apply the start or pause transition.

        Raises:
            TrackerCommandError: If persisted match state cannot transition.

        """
        match_data = context.match_data
        match_part = current_part(match_data)
        if match_part is None:
            if not MatchPart.objects.filter(match_data=match_data).exists():
                capture_starting_lineup(match_data)
            _prepare_new_part(match_data)
            MatchPart.objects.create(
                match_data=match_data,
                active=True,
                start_time=context.event_time,
                part_number=match_data.current_part,
            )
            return

        if (
            match_data.status != "active"
            or match_part.part_number != match_data.current_part
        ):
            raise TrackerCommandError(
                "Active match part does not match the match state.",
                code="invalid_match_state",
            )

        active_pause = Pause.objects.filter(
            match_data=match_data,
            active=True,
            match_part=match_part,
        ).first()

        if active_pause is not None and _timer_report_matches(
            SimpleEventObservation(
                match_data=match_data,
                match_part=match_part,
                reporting_team_id=context.team.pk,
                source_type="pause",
                record_filters={"active": True},
                effective_at=context.event_time,
            )
        ):
            return

        if active_pause is None:
            if _timer_report_matches(
                SimpleEventObservation(
                    match_data=match_data,
                    match_part=match_part,
                    reporting_team_id=context.team.pk,
                    source_type="pause",
                    record_filters={"active": False},
                    effective_at=context.event_time,
                )
            ):
                return
            if _timer_report_matches(
                SimpleEventObservation(
                    match_data=match_data,
                    match_part=match_part,
                    reporting_team_id=context.team.pk,
                    source_type="match_part",
                    record_filters={"active": True},
                    effective_at=context.event_time,
                )
            ):
                return

        if active_pause is None:
            Pause.objects.create(
                match_data=match_data,
                active=True,
                start_time=context.event_time,
                match_part=match_part,
            )
            return

        active_pause.active = False
        active_pause.end_time = max(
            context.event_time,
            active_pause.start_time or context.event_time,
        )
        active_pause.save(update_fields=["active", "end_time"])


@dataclass(frozen=True, slots=True)
class PartEndCommand:
    """Finish the active part and, when applicable, the match."""

    def apply(self, context: TrackerCommandContext) -> None:
        """Apply the part-end transition.

        Raises:
            TrackerCommandError: If no active part can be ended.

        """
        match_data = context.match_data
        match_part = current_part(match_data)
        last_part = match_part or (
            MatchPart.objects
            .filter(match_data=match_data)
            .order_by("-part_number")
            .first()
        )
        if (
            last_part is not None
            and not last_part.active
            and _timer_report_matches(
                SimpleEventObservation(
                    match_data=match_data,
                    match_part=last_part,
                    reporting_team_id=context.team.pk,
                    source_type="match_part",
                    record_filters={"active": False},
                    effective_at=context.event_time,
                )
            )
        ):
            return
        if match_data.status != "active":
            raise TrackerCommandError(
                "Only an active match can end a part.",
                code="match_not_active",
            )
        if match_part is None:
            raise TrackerCommandError(
                NO_ACTIVE_MATCH_PART_MESSAGE,
                code="no_active_part",
            )
        if match_part.part_number != match_data.current_part:
            raise TrackerCommandError(
                "Active match part does not match the match state.",
                code="invalid_match_state",
            )

        for active_pause in Pause.objects.filter(
            match_data=match_data,
            match_part=match_part,
            active=True,
        ):
            active_pause.active = False
            active_pause.end_time = max(
                context.event_time,
                active_pause.start_time or context.event_time,
            )
            active_pause.save(update_fields=["active", "end_time"])

        match_part.active = False
        match_part.end_time = max(context.event_time, match_part.start_time)
        match_part.save(update_fields=["active", "end_time"])

        if match_data.current_part < match_data.parts:
            match_data.current_part += 1
            match_data.save(update_fields=["current_part"])
            return

        match_data_uuid = match_data.id_uuid
        match_data_id = (
            match_data_uuid
            if isinstance(match_data_uuid, UUID)
            else UUID(str(match_data_uuid))
        )
        scores = compute_scores_for_matchdata_ids([match_data_id]).get(
            match_data_id, (0, 0)
        )
        match_data.status = "finished"
        match_data.home_score, match_data.away_score = scores
        match_data.save(update_fields=["status", "home_score", "away_score"])
        _enqueue_match_finished(
            match=context.match,
            match_data=match_data,
            jobs=context.jobs,
        )


def _enqueue_match_finished(
    *,
    match: Match,
    match_data: MatchData,
    jobs: TrackerJobDispatcher,
) -> None:
    def enqueue() -> None:
        try:
            jobs.match_finished(
                match_id=str(match.id_uuid),
                match_data_id=str(match_data.id_uuid),
            )
        except Exception:
            logger.warning(
                "Failed to enqueue match finished push task (http)",
                exc_info=True,
            )

    transaction.on_commit(enqueue)


@dataclass(frozen=True, slots=True)
class TimeoutCommand:
    """Register a timeout for either participating team."""

    for_team: bool

    def apply(self, context: TrackerCommandContext) -> None:
        """Register the timeout.

        Raises:
            TrackerCommandError: If play is paused or the timeout limit is reached.

        """
        timeout_team = (
            context.team if self.for_team else other_team(context.match, context.team)
        )
        match_part = current_part(context.match_data)
        plan = None
        if match_part is not None:
            plan = plan_simple_event_reconciliation(
                SimpleEventObservation(
                    match_data=context.match_data,
                    match_part=match_part,
                    reporting_team_id=context.team.pk,
                    source_type="timeout",
                    record_filters={"team_id": str(timeout_team.pk)},
                    effective_at=context.event_time,
                )
            )
            if plan.matched_event is not None:
                record_matched_observation(
                    event=plan.matched_event,
                    effective_at=context.event_time,
                    payload={
                        "kind": "timeout",
                        "timeout_team_id": str(timeout_team.pk),
                        "reporting_team_id": str(context.team.pk),
                    },
                )
                return

        match_part, _ = require_live_part(
            context.match_data,
            context.team,
            context.match,
        )
        if (
            Timeout.objects.filter(
                match_data=context.match_data,
                team=timeout_team,
            ).count()
            >= MAX_TIMEOUTS_PER_TEAM
        ):
            raise TrackerCommandError(
                "Maximum number of timeouts reached.",
                code="max_timeouts",
            )

        pause = Pause.objects.create(
            match_data=context.match_data,
            active=True,
            start_time=context.event_time,
            match_part=match_part,
        )
        timeout = Timeout.objects.create(
            match_data=context.match_data,
            match_part=match_part,
            team=timeout_team,
            pause=pause,
        )
        event = MatchEvent.objects.get(source_type="timeout", source_id=timeout.pk)
        create_reconciliation_candidates(
            event=event,
            possible_duplicates=plan.review_events if plan else (),
        )


@dataclass(frozen=True, slots=True)
class NewAttackCommand:
    """Register a new attack for the reporting team."""

    def apply(self, context: TrackerCommandContext) -> None:
        """Register the attack."""
        match_part, _ = require_live_part(
            context.match_data,
            context.team,
            context.match,
        )
        plan = plan_simple_event_reconciliation(
            SimpleEventObservation(
                match_data=context.match_data,
                match_part=match_part,
                reporting_team_id=context.team.pk,
                source_type="attack",
                record_filters={},
                effective_at=context.event_time,
            )
        )
        if plan.matched_event is not None:
            record_matched_observation(
                event=plan.matched_event,
                effective_at=context.event_time,
                payload={"kind": "attack", "reporting_team_id": str(context.team.pk)},
            )
            return
        attack = Attack.objects.create(
            match_data=context.match_data,
            match_part=match_part,
            team=context.team,
            time=context.event_time,
        )
        event = MatchEvent.objects.get(source_type="attack", source_id=attack.pk)
        create_reconciliation_candidates(
            event=event,
            possible_duplicates=plan.review_events,
        )
