# ruff: noqa: D103
"""Database-level regression tests for tracker timer invariants."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
import pytest

from apps.game_tracker.models import MatchData, MatchPart, Pause
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_match


@pytest.mark.django_db
def test_match_has_only_one_match_data_row() -> None:
    tracker = create_tracker_match(prefix="Unique Match Data")

    with pytest.raises(IntegrityError), transaction.atomic():
        MatchData.objects.create(match_link=tracker.match)


@pytest.mark.django_db
def test_match_has_only_one_active_part() -> None:
    tracker = create_tracker_match(prefix="Unique Active Part")
    MatchPart.objects.create(
        match_data=tracker.match_data,
        part_number=1,
        start_time=timezone.now(),
        active=True,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        MatchPart.objects.create(
            match_data=tracker.match_data,
            part_number=2,
            start_time=timezone.now(),
            active=True,
        )


@pytest.mark.django_db
def test_match_has_only_one_active_pause() -> None:
    tracker = create_tracker_match(prefix="Unique Active Pause")
    part = MatchPart.objects.create(
        match_data=tracker.match_data,
        part_number=1,
        start_time=timezone.now() - timedelta(minutes=1),
        active=True,
    )
    Pause.objects.create(
        match_data=tracker.match_data,
        match_part=part,
        start_time=timezone.now(),
        active=True,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        Pause.objects.create(
            match_data=tracker.match_data,
            match_part=part,
            start_time=timezone.now(),
            active=True,
        )
