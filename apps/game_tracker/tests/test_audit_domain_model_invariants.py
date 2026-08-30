"""Database-backed tests for tracker model invariants and temporal policy."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.utils import timezone
import pytest

from apps.game_tracker.models import (
    MatchEvent,
    MatchEventReconciliation,
    MatchEventReconciliationDecision,
    MatchPart,
    Pause,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_tracker_match,
)


@pytest.mark.django_db
def test_match_part_rejects_an_end_before_its_start() -> None:
    """A period cannot contribute negative elapsed match time."""
    tracker = create_tracker_match(prefix="Audit Part Chronology")
    started_at = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        MatchPart.objects.create(
            match_data=tracker.match_data,
            part_number=1,
            start_time=started_at,
            end_time=started_at - timedelta(microseconds=1),
        )

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.event_sequence == 0
    assert not MatchEvent.objects.filter(match_data=tracker.match_data).exists()


@pytest.mark.django_db
def test_match_part_number_is_unique_even_after_a_part_becomes_inactive() -> None:
    """A historical period number cannot be reused as a new timer segment."""
    tracker = create_tracker_match(prefix="Audit Part Identity")
    started_at = timezone.now()
    MatchPart.objects.create(
        match_data=tracker.match_data,
        part_number=1,
        start_time=started_at,
        end_time=started_at,
        active=False,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        MatchPart.objects.create(
            match_data=tracker.match_data,
            part_number=1,
            start_time=started_at + timedelta(seconds=1),
            active=False,
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("start_offset", "end_offset", "active"),
    [
        (None, None, True),
        (0, 1, True),
        (1, 0, False),
    ],
)
def test_pause_rejects_invalid_open_and_chronological_states(
    start_offset: int | None,
    end_offset: int | None,
    active: bool,
) -> None:
    """Active pauses stay open and completed pauses never run backwards."""
    tracker = create_tracker_match(prefix=f"Audit Pause {start_offset} {end_offset}")
    now = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        Pause.objects.create(
            match_data=tracker.match_data,
            start_time=(
                now + timedelta(seconds=start_offset)
                if start_offset is not None
                else None
            ),
            end_time=(
                now + timedelta(seconds=end_offset) if end_offset is not None else None
            ),
            active=active,
        )

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.event_sequence == 0
    assert not MatchEvent.objects.filter(match_data=tracker.match_data).exists()


def test_pause_length_is_zero_until_both_boundaries_exist() -> None:
    """Incomplete timer state cannot leak an invented duration."""
    start = timezone.now()

    assert Pause(start_time=None, end_time=start).length() == timedelta(0)
    assert Pause(start_time=start, end_time=None).length() == timedelta(0)
    assert Pause(
        start_time=start,
        end_time=start + timedelta(seconds=75),
    ).length() == timedelta(seconds=75)


def _event(*, tracker: TrackerMatchContext, sequence: int) -> MatchEvent:
    match_data = tracker.match_data
    return MatchEvent.objects.create(
        match_data=match_data,
        sequence=sequence,
        kind="audit",
        source_type="audit",
        source_id=uuid4(),
    )


@pytest.mark.django_db
def test_reconciliation_candidate_requires_two_distinct_events() -> None:
    """An event cannot be proposed as a duplicate of itself."""
    tracker = create_tracker_match(prefix="Audit Reconciliation Pair")
    event = _event(tracker=tracker, sequence=1)

    with pytest.raises(IntegrityError), transaction.atomic():
        MatchEventReconciliation.objects.create(
            match_data=tracker.match_data,
            first_event=event,
            second_event=event,
            confidence=100,
            reason="self pair",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("decision", "include_canonical"),
    [
        (MatchEventReconciliationDecision.DECISION_MERGE, False),
        (MatchEventReconciliationDecision.DECISION_SEPARATE, True),
    ],
)
def test_reconciliation_decision_requires_canonical_event_only_for_merges(
    decision: str,
    include_canonical: bool,
) -> None:
    """Merge and separate resolutions cannot persist contradictory state."""
    tracker = create_tracker_match(prefix=f"Audit Reconciliation {decision}")
    first = _event(tracker=tracker, sequence=1)
    second = _event(tracker=tracker, sequence=2)
    resolution = _event(tracker=tracker, sequence=3)
    reconciliation = MatchEventReconciliation.objects.create(
        match_data=tracker.match_data,
        first_event=first,
        second_event=second,
        confidence=80,
        reason="audit candidate",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        MatchEventReconciliationDecision.objects.create(
            reconciliation=reconciliation,
            decision=decision,
            canonical_event=first if include_canonical else None,
            resolution_event=resolution,
        )
