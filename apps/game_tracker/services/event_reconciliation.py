"""Reconcile independent reports of the same real-world match event."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from bg_uuidv7 import uuidv7
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchEvent,
    MatchEventObservation,
    MatchEventReconciliation,
    MatchEventReconciliationDecision,
    MatchPart,
    Shot,
    ShotEventDetail,
)
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES
from apps.game_tracker.services.lineup_projections import rebuild_match_projections
from apps.game_tracker.services.live_updates import record_match_change
from apps.game_tracker.services.match_event_context import (
    current_match_event_context,
    match_event_context,
)

from .match_events import _elapsed_ms


AUTO_MATCH_WINDOW = timedelta(seconds=3)
REVIEW_WINDOW = timedelta(seconds=15)


class EventReconciliationError(RuntimeError):
    """Raised when a reconciliation decision cannot be applied."""


@dataclass(frozen=True, slots=True)
class ShotReconciliationPlan:
    """Result of comparing one report with existing canonical shots."""

    matched_event: MatchEvent | None
    review_events: tuple[MatchEvent, ...]


@dataclass(frozen=True, slots=True)
class ShotObservation:
    """Canonical semantics shared by candidate selection and persistence."""

    match_data: MatchData
    match_part: MatchPart
    reporting_team_id: object
    shooting_team_id: object
    outcome: str
    shot_type: GoalType | None
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationResolution:
    """Validated intent for one pending duplicate candidate."""

    match_data: MatchData
    reconciliation_id: object
    decision: str
    canonical_event_id: object | None
    actor: object | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _ResolutionEvent:
    """Fields recorded in the immutable resolution fact."""

    decision: str
    canonical_event: MatchEvent | None
    actor: object | None
    reason: str


def _candidate_shots(
    observation: ShotObservation,
    *,
    window: timedelta,
) -> QuerySet[ShotEventDetail]:
    """Return semantically compatible reports from the other team."""
    return (
        ShotEventDetail.objects
        .select_related("event")
        .filter(
            event__match_data=observation.match_data,
            event__match_part=observation.match_part,
            event__status=MatchEvent.STATUS_ACTIVE,
            event__kind="shot.created",
            event__effective_at__gte=observation.effective_at - window,
            event__effective_at__lte=observation.effective_at + window,
            shooting_team_id=observation.shooting_team_id,
            outcome=observation.outcome,
            shot_type=observation.shot_type,
        )
        .exclude(event__observations__reporting_team_id=observation.reporting_team_id)
        .filter(event__observations__reporting_team__isnull=False)
        .distinct()
        .order_by("event__sequence")
    )


def plan_shot_reconciliation(
    observation: ShotObservation,
) -> ShotReconciliationPlan:
    """Auto-match one unambiguous close report or request human review."""
    exact = list(
        _candidate_shots(
            observation,
            window=AUTO_MATCH_WINDOW,
        )
    )
    if len(exact) == 1:
        return ShotReconciliationPlan(
            matched_event=exact[0].event,
            review_events=(),
        )

    review = list(
        _candidate_shots(
            observation,
            window=REVIEW_WINDOW,
        )
    )
    return ShotReconciliationPlan(
        matched_event=None,
        review_events=tuple(detail.event for detail in review),
    )


def record_matched_observation(
    *,
    event: MatchEvent,
    effective_at: datetime,
    payload: dict[str, Any],
) -> MatchEventObservation:
    """Attach the current client report without creating another score fact."""
    context = current_match_event_context()
    return MatchEventObservation.objects.create(
        match_data=event.match_data,
        event=event,
        command_id=context.command_id,
        reporting_team=(
            context.source_team
            if getattr(context.source_team, "pk", None) is not None
            else None
        ),
        actor=(
            context.actor if getattr(context.actor, "is_authenticated", False) else None
        ),
        source=context.source,
        device_id=context.device_id,
        session_id=context.session_id,
        client_sequence=context.client_sequence,
        effective_at=effective_at,
        elapsed_ms=_elapsed_ms(event.match_data, cast(Any, event), effective_at),
        origin=MatchEventObservation.ORIGIN_MATCHED,
        payload=payload,
    )


def create_reconciliation_candidates(
    *,
    event: MatchEvent,
    possible_duplicates: tuple[MatchEvent, ...],
) -> list[MatchEventReconciliation]:
    """Persist normalized ambiguous pairs for an explicit decision."""
    created: list[MatchEventReconciliation] = []
    for other in possible_duplicates:
        first, second = sorted((event, other), key=lambda item: item.sequence)
        delta_ms = abs(
            int(
                (
                    (event.effective_at or event.recorded_at)
                    - (other.effective_at or other.recorded_at)
                ).total_seconds()
                * 1_000
            )
        )
        confidence = max(1, 100 - min(99, delta_ms // 150))
        candidate, was_created = MatchEventReconciliation.objects.get_or_create(
            match_data=event.match_data,
            first_event=first,
            second_event=second,
            defaults={
                "confidence": confidence,
                "reason": f"compatible shot reports {delta_ms}ms apart",
            },
        )
        if was_created:
            created.append(candidate)
    return created


def _event_summary(event: MatchEvent) -> dict[str, object]:
    detail: dict[str, object] = {}
    source_team_id = getattr(event, "source_team_id", None)
    if hasattr(event, "shot_detail"):
        shot = cast(Any, event).shot_detail
        detail = {
            "shooting_team_id": (
                str(shot.shooting_team_id) if shot.shooting_team_id else None
            ),
            "shooter_id": str(shot.shooter_id) if shot.shooter_id else None,
            "defender_id": str(shot.defender_id) if shot.defender_id else None,
            "shot_type_id": str(shot.shot_type_id) if shot.shot_type_id else None,
            "outcome": shot.outcome,
        }
    return {
        "event_id": str(event.pk),
        "logical_event_id": str(event.logical_id),
        "sequence": event.sequence,
        "kind": event.kind,
        "effective_at": event.effective_at.isoformat() if event.effective_at else None,
        "elapsed_ms": event.elapsed_ms,
        "source_team_id": str(source_team_id) if source_team_id else None,
        "detail": detail,
    }


def pending_reconciliations(match_data: MatchData) -> list[dict[str, object]]:
    """Return unresolved candidate pairs with enough context for human review."""
    candidates = (
        MatchEventReconciliation.objects
        .filter(match_data=match_data, decision__isnull=True)
        .select_related(
            "first_event",
            "first_event__shot_detail",
            "second_event",
            "second_event__shot_detail",
        )
        .order_by("-confidence", "created_at")
    )
    return [
        {
            "id_uuid": str(candidate.pk),
            "confidence": candidate.confidence,
            "reason": candidate.reason,
            "first_event": _event_summary(candidate.first_event),
            "second_event": _event_summary(candidate.second_event),
        }
        for candidate in candidates
    ]


def _append_resolution_event(
    match_data: MatchData,
    reconciliation: MatchEventReconciliation,
    resolution: _ResolutionEvent,
) -> MatchEvent:
    match_data.refresh_from_db(fields=["event_sequence"])
    match_data.event_sequence += 1
    MatchData.objects.filter(pk=match_data.pk).update(
        event_sequence=match_data.event_sequence
    )
    return MatchEvent.objects.create(
        match_data=match_data,
        sequence=match_data.event_sequence,
        logical_id=(
            resolution.canonical_event.logical_id
            if resolution.canonical_event
            else uuidv7()
        ),
        kind=f"reconciliation.{resolution.decision}",
        source_type="reconciliation",
        source_id=reconciliation.pk,
        effective_at=timezone.now(),
        actor=(
            resolution.actor
            if getattr(resolution.actor, "is_authenticated", False)
            else None
        ),
        source="reconciliation",
        payload={
            "decision": resolution.decision,
            "candidate_id": str(reconciliation.pk),
            "first_event_id": str(reconciliation.first_event_id),
            "second_event_id": str(reconciliation.second_event_id),
            "canonical_event_id": (
                str(resolution.canonical_event.pk)
                if resolution.canonical_event is not None
                else None
            ),
            "reason": resolution.reason,
        },
    )


def resolve_reconciliation(
    resolution: ReconciliationResolution,
) -> MatchEventReconciliationDecision:
    """Resolve one candidate once and rebuild affected projections.

    Raises:
        EventReconciliationError: If the candidate or decision is invalid.

    """
    if resolution.decision not in {
        MatchEventReconciliationDecision.DECISION_MERGE,
        MatchEventReconciliationDecision.DECISION_SEPARATE,
    }:
        raise EventReconciliationError("Decision must be 'merge' or 'separate'.")

    with transaction.atomic():
        locked = MatchData.objects.select_for_update().get(pk=resolution.match_data.pk)
        reconciliation = (
            MatchEventReconciliation.objects
            .select_related("first_event", "second_event")
            .filter(pk=resolution.reconciliation_id, match_data=locked)
            .first()
        )
        if reconciliation is None:
            raise EventReconciliationError("Reconciliation candidate not found.")
        if MatchEventReconciliationDecision.objects.filter(
            reconciliation=reconciliation
        ).exists():
            raise EventReconciliationError("Reconciliation is already resolved.")

        canonical_event: MatchEvent | None = None
        duplicate: MatchEvent | None = None
        if resolution.decision == MatchEventReconciliationDecision.DECISION_MERGE:
            allowed = {
                str(reconciliation.first_event_id): reconciliation.first_event,
                str(reconciliation.second_event_id): reconciliation.second_event,
            }
            selected_id = str(
                resolution.canonical_event_id or reconciliation.first_event_id
            )
            canonical_event = allowed.get(selected_id)
            if canonical_event is None:
                raise EventReconciliationError(
                    "canonical_event_id must belong to the candidate pair."
                )
            duplicate = (
                reconciliation.second_event
                if canonical_event.pk == reconciliation.first_event_id
                else reconciliation.first_event
            )
            if duplicate.source_type != "shot":
                raise EventReconciliationError(
                    "Only shot reconciliation is supported currently."
                )
            projection = Shot.objects.filter(
                pk=duplicate.source_id,
                match_data=locked,
            ).first()
            if projection is None:
                raise EventReconciliationError(
                    "Duplicate shot projection is no longer active."
                )
            with match_event_context(
                actor=resolution.actor,
                source="reconciliation",
            ):
                projection.delete()
                rebuild_match_projections(locked)

        resolution_event = _append_resolution_event(
            locked,
            reconciliation,
            _ResolutionEvent(
                decision=resolution.decision,
                canonical_event=canonical_event,
                actor=resolution.actor,
                reason=resolution.reason,
            ),
        )
        result = MatchEventReconciliationDecision.objects.create(
            reconciliation=reconciliation,
            decision=resolution.decision,
            canonical_event=canonical_event,
            resolution_event=resolution_event,
            actor=(
                resolution.actor
                if getattr(resolution.actor, "is_authenticated", False)
                else None
            ),
            reason=resolution.reason,
        )
        if duplicate is not None:
            assert canonical_event is not None
            related = (
                MatchEventReconciliation.objects
                .filter(
                    Q(first_event=duplicate) | Q(second_event=duplicate),
                    decision__isnull=True,
                )
                .exclude(pk=reconciliation.pk)
                .select_related("first_event", "second_event")
            )
            for stale in related:
                stale_reason = (
                    f"Automatically separated after {duplicate.pk} was merged "
                    f"into {canonical_event.pk}."
                )
                stale_event = _append_resolution_event(
                    locked,
                    stale,
                    _ResolutionEvent(
                        decision=(MatchEventReconciliationDecision.DECISION_SEPARATE),
                        canonical_event=None,
                        actor=resolution.actor,
                        reason=stale_reason,
                    ),
                )
                MatchEventReconciliationDecision.objects.create(
                    reconciliation=stale,
                    decision=MatchEventReconciliationDecision.DECISION_SEPARATE,
                    canonical_event=None,
                    resolution_event=stale_event,
                    actor=(
                        resolution.actor
                        if getattr(resolution.actor, "is_authenticated", False)
                        else None
                    ),
                    reason=stale_reason,
                )
        record_match_change(locked, resources=set(ALL_LIVE_RESOURCES))
        return result
