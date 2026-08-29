"""Tests for the read-only impact-version comparison command."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GoalType, MatchData, MatchPart, Shot
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
def test_compare_match_impacts_reports_multiple_matches_and_players() -> None:
    """The report compares player rows across more than one match."""
    home = Team.objects.create(name="Home", club=Club.objects.create(name="Home Club"))
    away = Team.objects.create(name="Away", club=Club.objects.create(name="Away Club"))
    today = timezone.now().date()
    season = Season.objects.create(name="Comparison", start_date=today, end_date=today)
    attacker = get_user_model().objects.create_user(username="attacker").player
    defender = get_user_model().objects.create_user(username="defender").player
    shot_type = GoalType.objects.create(name="Afstand schot")

    match_data_ids: list[str] = []
    for offset in (1, 2):
        start = timezone.now() - timedelta(days=offset)
        match = Match.objects.create(
            home_team=home,
            away_team=away,
            season=season,
            start_time=start,
        )
        match_data = MatchData.objects.get(match_link=match)
        match_data.status = "finished"
        match_data.save(update_fields=["status"])
        match_data_ids.append(str(match_data.id_uuid))
        part = MatchPart.objects.create(
            match_data=match_data,
            part_number=1,
            start_time=start,
            active=True,
        )
        Shot.objects.create(
            player=attacker,
            match_data=match_data,
            match_part=part,
            team=home,
            for_team=True,
            scored=True,
            shot_type=shot_type,
            time=start + timedelta(minutes=1),
        )
        Shot.objects.create(
            player=defender,
            match_data=match_data,
            match_part=part,
            team=home,
            for_team=False,
            scored=True,
            shot_type=shot_type,
            time=start + timedelta(minutes=2),
        )

    stdout = StringIO()
    call_command(
        "compare_match_impacts",
        *(
            argument
            for match_id in match_data_ids
            for argument in ("--match-data-id", match_id)
        ),
        "--format",
        "json",
        stdout=stdout,
    )
    payload = json.loads(stdout.getvalue())

    assert {row["match_data_id"] for row in payload} == set(match_data_ids)
    assert {row["player"] for row in payload} == {"attacker", "defender"}
    defender_rows = [row for row in payload if row["player"] == "defender"]
    assert all(row["v6"] > 0 for row in defender_rows)
    assert all(row["v7_gax"] < 0 for row in defender_rows)
