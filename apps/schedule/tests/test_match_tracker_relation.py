"""Tests for the one-to-one match tracker relation."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
def test_match_tracker_data_can_be_eagerly_loaded_without_an_extra_query(
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    """The semantic one-to-one relation should remove endpoint-side lookups."""
    season = Season.objects.create(
        name="Tracker relation season",
        start_date=timezone.localdate(),
        end_date=timezone.localdate(),
    )
    home_club = Club.objects.create(name="Tracker relation home")
    away_club = Club.objects.create(name="Tracker relation away")
    match = Match.objects.create(
        home_team=Team.objects.create(name="Home", club=home_club),
        away_team=Team.objects.create(name="Away", club=away_club),
        season=season,
        start_time=timezone.now(),
    )

    loaded_match = Match.objects.select_related("tracker_data").get(pk=match.pk)

    with django_assert_num_queries(0):
        tracker_data = loaded_match.tracker_data

    assert tracker_data.match_link_id == match.pk
