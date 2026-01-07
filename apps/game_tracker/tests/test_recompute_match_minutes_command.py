"""Tests for the recompute_match_minutes management command."""

from __future__ import annotations

from datetime import timedelta
import io

from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.management.commands import recompute_match_minutes
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
def test_recompute_match_minutes_dry_run_computes_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry run should compute rows but not persist any minutes."""
    season = Season.objects.create(
        name="2025",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    club = Club.objects.create(name="Test Club")
    opp_club = Club.objects.create(name="Opponent Club")
    team = Team.objects.create(name="Team", club=club)
    opponent = Team.objects.create(name="Opponent", club=opp_club)

    match = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now() - timedelta(hours=2),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save()

    def fake_compute_minutes_by_player_id(*, match_data: MatchData) -> dict[str, float]:
        return {
            "player-1": 10.0,
            "player-2": 0.0,
        }

    def fake_persist_match_minutes(*, match_data: MatchData) -> int:
        raise AssertionError("persist_match_minutes should not be called in --dry-run")

    monkeypatch.setattr(
        recompute_match_minutes,
        "compute_minutes_by_player_id",
        fake_compute_minutes_by_player_id,
    )
    monkeypatch.setattr(
        recompute_match_minutes,
        "persist_match_minutes",
        fake_persist_match_minutes,
    )

    out = io.StringIO()

    call_command(
        "recompute_match_minutes",
        match_data_id=str(match_data.id_uuid),
        dry_run=True,
        stdout=out,
    )

    text = out.getvalue()
    assert "would upsert 1 rows" in text
    assert "Done. Processed 1 matches." in text


@pytest.mark.django_db
def test_recompute_match_minutes_writes_rows_when_not_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-dry-run should persist and report upserted row count."""
    season = Season.objects.create(
        name="2025",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    club = Club.objects.create(name="Test Club")
    opp_club = Club.objects.create(name="Opponent Club")
    team = Team.objects.create(name="Team", club=club)
    opponent = Team.objects.create(name="Opponent", club=opp_club)

    match = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now() - timedelta(hours=2),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save()

    def fake_persist_match_minutes(*, match_data: MatchData) -> int:
        return 3

    monkeypatch.setattr(
        recompute_match_minutes,
        "persist_match_minutes",
        fake_persist_match_minutes,
    )

    out = io.StringIO()

    call_command(
        "recompute_match_minutes",
        match_data_id=str(match_data.id_uuid),
        dry_run=False,
        stdout=out,
    )

    text = out.getvalue()
    assert ": 3 rows" in text
    assert "upserted 3 rows" in text
