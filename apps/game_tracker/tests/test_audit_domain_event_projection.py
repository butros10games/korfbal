"""Audit tests for event-first bulk projection mutation contracts."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from django.utils import timezone
import pytest

from apps.game_tracker.models import Attack, MatchEvent, MatchEventObservation
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_match


EVENT_COUNT = 2


@pytest.mark.django_db
@pytest.mark.parametrize("use_bulk_update", [False, True])
def test_multi_row_updates_append_one_version_per_projection(
    use_bulk_update: bool,
) -> None:
    """Bulk mutation keeps every projection on its canonical event chain."""
    tracker = create_tracker_match(prefix=f"Audit Update {use_bulk_update}")
    attacks = [
        cast(
            Attack,
            Attack.objects.create(
                match_data=tracker.match_data,
                team=tracker.home_team,
                time=timezone.now(),
            ),
        )
        for _ in range(EVENT_COUNT)
    ]
    created_events = {
        event.source_id: event
        for event in MatchEvent.objects.filter(match_data=tracker.match_data)
    }

    if use_bulk_update:
        for attack in attacks:
            attack.team = tracker.away_team
        updated_count = Attack.objects.bulk_update(attacks, ["team"])
    else:
        updated_count = Attack.objects.filter(
            pk__in=[attack.pk for attack in attacks]
        ).update(team=tracker.away_team)

    assert updated_count == EVENT_COUNT
    assert not Attack.objects.filter(
        pk__in=[attack.pk for attack in attacks],
        team=tracker.home_team,
    ).exists()
    updated_events = list(
        MatchEvent.objects.filter(
            match_data=tracker.match_data,
            kind="attack.updated",
        ).order_by("sequence")
    )
    assert len(updated_events) == EVENT_COUNT
    assert all(
        event.supersedes == created_events[event.source_id]
        and event.logical_id == created_events[event.source_id].logical_id
        and event.payload["record"]["team_id"] == str(tracker.away_team.pk)
        for event in updated_events
    )
    assert (
        MatchEventObservation.objects.filter(event__in=updated_events).count()
        == EVENT_COUNT
    )


@pytest.mark.django_db
def test_queryset_delete_retracts_every_projection_before_removing_rows() -> None:
    """A multi-row delete remains visible as two immutable retractions."""
    tracker = create_tracker_match(prefix="Audit Queryset Delete")
    attacks = cast(
        list[Attack],
        Attack.objects.bulk_create([
            Attack(
                match_data=tracker.match_data,
                team=tracker.home_team,
                time=timezone.now(),
            ),
            Attack(
                match_data=tracker.match_data,
                team=tracker.away_team,
                time=timezone.now(),
            ),
        ]),
    )
    roots = {
        event.source_id: event.logical_id
        for event in MatchEvent.objects.filter(match_data=tracker.match_data)
    }

    Attack.objects.filter(pk__in=[attack.pk for attack in attacks]).delete()

    assert not Attack.objects.filter(pk__in=[attack.pk for attack in attacks]).exists()
    retractions = list(
        MatchEvent.objects.filter(
            match_data=tracker.match_data,
            kind="attack.retracted",
        )
    )
    assert len(retractions) == EVENT_COUNT
    assert all(event.logical_id == roots[event.source_id] for event in retractions)
    assert (
        MatchEventObservation.objects.filter(event__in=retractions).count()
        == EVENT_COUNT
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "bulk_write",
    [
        pytest.param(
            lambda objects: Attack.objects.bulk_create(
                objects,
                ignore_conflicts=True,
            ),
            id="ignore-conflicts",
        ),
        pytest.param(
            lambda objects: Attack.objects.bulk_create(
                objects,
                update_conflicts=True,
                update_fields=["team"],
                unique_fields=["pk"],
            ),
            id="update-conflicts",
        ),
    ],
)
def test_conflict_handling_bulk_creates_are_rejected_without_partial_events(
    bulk_write: Callable[[list[Attack]], object],
) -> None:
    """Ambiguous conflict handling cannot create projections or envelopes."""
    tracker = create_tracker_match(prefix="Audit Bulk Conflict")
    objects = [
        Attack(
            match_data=tracker.match_data,
            team=tracker.home_team,
            time=timezone.now(),
        )
    ]

    with pytest.raises(
        ValueError,
        match="Conflict-handling bulk writes cannot preserve event identity",
    ):
        bulk_write(objects)

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.event_sequence == 0
    assert not Attack.objects.filter(match_data=tracker.match_data).exists()
    assert not MatchEvent.objects.filter(match_data=tracker.match_data).exists()


@pytest.mark.django_db
def test_bulk_update_without_fields_rejects_without_appending_a_phantom_event() -> None:
    """The event-aware manager preserves Django's empty-field contract."""
    tracker = create_tracker_match(prefix="Audit Empty Bulk Update")
    attack = cast(
        Attack,
        Attack.objects.create(
            match_data=tracker.match_data,
            team=tracker.home_team,
            time=timezone.now(),
        ),
    )
    event_ids_before = list(
        MatchEvent.objects.filter(match_data=tracker.match_data).values_list(
            "pk", flat=True
        )
    )
    tracker.match_data.refresh_from_db()
    event_sequence_before = tracker.match_data.event_sequence

    with pytest.raises(ValueError, match="Field names must be given to bulk_update"):
        Attack.objects.bulk_update([attack], fields=[])

    tracker.match_data.refresh_from_db()
    assert tracker.match_data.event_sequence == event_sequence_before
    assert (
        list(
            MatchEvent.objects.filter(match_data=tracker.match_data).values_list(
                "pk", flat=True
            )
        )
        == event_ids_before
    )
