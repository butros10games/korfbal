"""Tests for match summary helpers.

These helpers are used by multiple overview endpoints. They must stay cheap
because they run for every match shown on user/club pages.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, Shot
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
def test_build_match_summaries_uses_matchdata_scores(
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    """Match summaries should use MatchData scores and not query per match."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today,
        end_date=today,
    )

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)

    user = get_user_model().objects.create_user(
        username="scorer",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    # Ensure Match.get_final_score() would differ if it were called.
    # If build_match_summaries used Match.get_final_score, it would run extra
    # queries and (depending on the shots) may return different values.
    Shot.objects.create(
        match_data=match_data,
        team=home_team,
        player=player,
        scored=True,
    )
    Shot.objects.create(
        match_data=match_data,
        team=away_team,
        player=player,
        scored=True,
    )

    # The summary should use the denormalized MatchData scores.
    match_data.home_score = 7
    match_data.away_score = 4
    match_data.save(update_fields=["home_score", "away_score"])

    qs = MatchData.objects.select_related(
        "match_link",
        "match_link__home_team",
        "match_link__home_team__club",
        "match_link__away_team",
        "match_link__away_team__club",
        "match_link__season",
    ).filter(id_uuid=match_data.id_uuid)

    with django_assert_num_queries(1):
        entries = list(qs)

    with django_assert_num_queries(0):
        payload = build_match_summaries(entries)

    assert payload[0]["score"] == {"home": 7, "away": 4}


@pytest.mark.django_db
def test_build_match_summaries_finished_match_uses_shot_aggregate(
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    """Finished matches should use persisted MatchData scores (no shot query)."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today,
        end_date=today,
    )

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )
    match_data = MatchData.objects.get(match_link=match)

    user = get_user_model().objects.create_user(
        username="scorer2",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    # Stored score should be used for finished matches.
    match_data.status = "finished"
    match_data.home_score = 7
    match_data.away_score = 4
    match_data.save(update_fields=["status", "home_score", "away_score"])

    # Even if shots exist, finished summaries must not recompute scores.
    Shot.objects.create(
        match_data=match_data,
        team=home_team,
        player=player,
        scored=True,
    )
    Shot.objects.create(
        match_data=match_data,
        team=away_team,
        player=player,
        scored=True,
    )

    qs = MatchData.objects.select_related(
        "match_link",
        "match_link__home_team",
        "match_link__home_team__club",
        "match_link__away_team",
        "match_link__away_team__club",
        "match_link__season",
    ).filter(id_uuid=match_data.id_uuid)

    with django_assert_num_queries(1):
        entries = list(qs)

    # No extra query: finished uses persisted MatchData scores.
    with django_assert_num_queries(0):
        payload = build_match_summaries(entries)

    assert payload[0]["score"] == {"home": 7, "away": 4}


@pytest.mark.django_db
def test_build_match_summaries_active_match_uses_shots(
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    """Active matches should show current score derived from shots."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today,
        end_date=today,
    )

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )
    match_data = MatchData.objects.get(match_link=match)

    user = get_user_model().objects.create_user(
        username="scorer3",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    match_data.status = "active"
    match_data.home_score = 0
    match_data.away_score = 0
    match_data.save(update_fields=["status", "home_score", "away_score"])

    for _ in range(3):
        Shot.objects.create(
            match_data=match_data,
            team=home_team,
            player=player,
            scored=True,
        )

    for _ in range(2):
        Shot.objects.create(
            match_data=match_data,
            team=away_team,
            player=player,
            scored=True,
        )

    qs = MatchData.objects.select_related(
        "match_link",
        "match_link__home_team",
        "match_link__home_team__club",
        "match_link__away_team",
        "match_link__away_team__club",
        "match_link__season",
    ).filter(id_uuid=match_data.id_uuid)

    with django_assert_num_queries(1):
        entries = list(qs)

    # One aggregate query for active scores.
    with django_assert_num_queries(1):
        payload = build_match_summaries(entries)

    assert payload[0]["score"] == {"home": 3, "away": 2}
