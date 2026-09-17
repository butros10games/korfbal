"""Latest-event reads preserve undo order without materializing the full history."""

from datetime import timedelta
import tracemalloc
from uuid import uuid4

from django.db import models
from django.utils import timezone
import pytest

from apps.game_tracker.models import Attack, MatchData, MatchEvent
from apps.game_tracker.services.tracker_event_queries import last_event_model
from apps.game_tracker.tests.tracker_test_helpers import (
    create_match_part,
    create_tracker_match,
)


pytestmark = pytest.mark.django_db
LATEST_READ_MEMORY_LIMIT = 512 * 1024
MISSING_CANDIDATES = 65


@pytest.mark.parametrize("change", ["created", "corrected", "retracted"])
def test_latest_event_uses_committed_versions_not_client_time(change: str) -> None:
    """Corrections advance an event, and retractions hide every older version."""
    tracker = create_tracker_match(prefix="Latest committed event")
    first = Attack.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        time=timezone.now() + timedelta(minutes=10),
    )
    second = Attack.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        time=timezone.now() - timedelta(minutes=10),
    )
    expected = second
    if change == "corrected":
        first.time = timezone.now() - timedelta(minutes=20)
        first.save(update_fields=["time"])
        expected = first
    elif change == "retracted":
        second.delete()
        expected = first
    assert last_event_model(tracker.match_data) == expected


@pytest.mark.parametrize("has_start", [False, True])
@pytest.mark.parametrize("ended", [False, True])
def test_part_corrections_preserve_original_start_order(
    has_start: bool, ended: bool
) -> None:
    """An end is newest; a reopened start stays behind play unless history is absent."""
    tracker = create_tracker_match(prefix="Latest period event")
    part = create_match_part(
        match_data=tracker.match_data, start_offset=-timedelta(minutes=10)
    )
    if not has_start:
        MatchEvent.objects.filter(
            match_data=tracker.match_data,
            source_id=part.pk,
            kind="match_part.started",
        ).update(kind="match_part.created")
    attack = Attack.objects.create(
        match_data=tracker.match_data,
        match_part=part,
        team=tracker.home_team,
        time=timezone.now(),
    )
    part.end_time = timezone.now()
    part.active = False
    part.save(update_fields=["end_time", "active"])
    if not ended:
        part.end_time = None
        part.active = True
        part.save(update_fields=["end_time", "active"])
    expected = attack if has_start and not ended else part
    assert last_event_model(tracker.match_data) == expected


def test_latest_event_skips_missing_and_other_match_sources_across_batches() -> None:
    """A missing newest source must not truncate the search or escape match scope."""
    tracker = create_tracker_match(prefix="Latest available event")
    expected = Attack.objects.create(match_data=tracker.match_data, time=timezone.now())
    other = create_tracker_match(prefix="Other latest event")
    wrong_match = Attack.objects.create(
        match_data=other.match_data, time=timezone.now()
    )
    sequence = MatchEvent.objects.get(
        match_data=tracker.match_data, source_id=expected.pk
    ).sequence
    MatchEvent.objects.bulk_create([
        MatchEvent(
            match_data=tracker.match_data,
            sequence=sequence + offset + 1,
            source_type="attack",
            source_id=source_id,
            kind="attack.created",
        )
        for offset, source_id in enumerate([
            wrong_match.pk,
            *(uuid4() for _ in range(MISSING_CANDIDATES)),
        ])
    ])
    assert last_event_model(tracker.match_data) == expected


def test_latest_event_returns_none_after_all_sources_are_retracted() -> None:
    """Retracted events do not revive an older source or another match's history."""
    tracker = create_tracker_match(prefix="Retracted latest event")
    part = create_match_part(match_data=tracker.match_data)
    attack = Attack.objects.create(match_data=tracker.match_data, match_part=part)
    attack.delete()
    part.delete()
    other = create_tracker_match(prefix="Independent latest event")
    create_match_part(match_data=other.match_data)
    assert last_event_model(tracker.match_data) is None


@pytest.mark.parametrize("size", [2_000, 10_000])
@pytest.mark.parametrize("reopened", [False, True])
def test_latest_event_python_memory_does_not_scale_with_history(
    size: int, reopened: bool
) -> None:
    """A valid newest source can be read without retaining all prior event tuples."""
    tracker = create_tracker_match(prefix=f"Latest history {size}")
    part = create_match_part(match_data=tracker.match_data)
    # Seed the read model directly: per-event publication is outside this read test.
    attacks = models.QuerySet(model=Attack).bulk_create([
        Attack(match_data=tracker.match_data, match_part=part, team=tracker.home_team)
        for _ in range(size)
    ])
    MatchEvent.objects.bulk_create([
        MatchEvent(
            match_data=tracker.match_data,
            sequence=index + 2,
            source_type="attack",
            source_id=attack.pk,
            kind="attack.created",
        )
        for index, attack in enumerate(attacks)
    ])
    expected = attacks[-1]
    if reopened:
        MatchData.objects.filter(pk=tracker.match_data.pk).update(
            event_sequence=size + 1
        )
        part.active = False
        part.end_time = timezone.now()
        part.save(update_fields=["active", "end_time"])
        part.active = True
        part.end_time = None
        part.save(update_fields=["active", "end_time"])
    tracemalloc.start()
    try:
        actual = last_event_model(tracker.match_data)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    assert peak_bytes < LATEST_READ_MEMORY_LIMIT
