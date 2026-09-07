"""Synthetic regression fixtures for provider identities and score corrections."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

from django.utils import timezone
import pytest

from apps.competition.models import (
    Club,
    Match,
    Pool,
    PoolEntry,
    ResultRevision,
    SyncResource,
    Team,
)
from apps.competition.services.importer import Importer
from apps.schedule.models import Season


def team_payload(identifier: str) -> dict[str, Any]:
    """Use fabricated teams rather than captured names or credentials."""
    return {
        "PublicTeamId": identifier,
        "TeamName": f"Example {identifier}",
        "SportId": "KORFBALL-VE-WK",
        "Club": {
            "ClubId": f"C{identifier}",
            "ClubName": f"Club {identifier}",
            "City": "Example",
        },
    }


def match_payload() -> dict[str, Any]:
    """Return a completed score with the structure seen in collection feeds."""
    return {
        "PublicMatchId": "M1",
        "MatchDateTime": "2026-09-05T13:30:00+0200",
        "Status": "FINAL",
        "HomeTeam": team_payload("T1"),
        "AwayTeam": team_payload("T2"),
        "Pool": {"PoolId": 10, "PoolName": "A-1", "ClassName": "Example class"},
        "HomeResult": {"Score": 0},
        "AwayResult": {"Score": 10},
        "AutoResult": None,
    }


@pytest.mark.django_db
def test_idempotent_results_and_discovery(season: Season) -> None:
    """Overlapping club/poule results never duplicate identities or revisions."""
    importer = Importer(season, timezone.now())
    payload = {"MatchResult": [match_payload(), match_payload()]}
    importer.apply("club_results", "CT1", payload)
    queued = SyncResource.objects.count()
    importer.apply("club_results", "CT2", payload)
    assert Match.objects.count() == 1
    assert ResultRevision.objects.count() == 1
    assert Match.objects.get().home_score == 0
    assert Team.objects.count() == len({"T1", "T2"})
    assert Club.objects.count() == len({"CT1", "CT2"})
    assert SyncResource.objects.count() == queued


@pytest.mark.django_db
def test_corrections_and_stale_observations(season: Season) -> None:
    """New scores get an audit record; old observations and fixture summaries lose."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "CT1", {"MatchResult": [row]})
    corrected = deepcopy(row)
    corrected["HomeResult"]["Score"] = 12
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_results", "CT1", {"MatchResult": [corrected]}
    )
    Importer(season, now).apply("club_results", "CT1", {"MatchResult": [row]})
    row["Status"] = "SCHEDULED"
    Importer(season, now + timedelta(seconds=2)).apply(
        "club_program", "CT1", {"ProgramItemMatchClub": [{"Match": row}]}
    )
    match = Match.objects.get()
    assert match.home_score == corrected["HomeResult"]["Score"]
    assert match.status == "FINAL"
    assert match.revisions.count() == len([row, corrected])


@pytest.mark.django_db
def test_season_boundaries_and_sport_identity(season: Season) -> None:
    """Reject earlier seasons and retain separate indoor/outdoor source IDs."""
    importer = Importer(season, timezone.now())
    old = match_payload()
    old["MatchDateTime"] = "2025-09-01T10:00:00+0200"
    importer.apply("club_results", "CT1", {"MatchResult": [old]})
    assert not Match.objects.exists()
    indoor = team_payload("INDOOR")
    indoor["SportId"] = "KORFBALL-ZA-WK"
    indoor["TeamName"] = team_payload("T1")["TeamName"]
    importer.apply("club_teams", "CT1", {"ClubTeam": [team_payload("T1"), indoor]})
    assert Team.objects.count() == len({"T1", "T2"})


@pytest.mark.django_db
def test_official_standings_and_filtered_coverage(season: Season) -> None:
    """Preserve deductions and provider filtering instead of inventing a full table."""
    importer = Importer(season, timezone.now())
    importer.pool({"PoolId": 10})
    row = {
        **team_payload("T1"),
        "Position": 1,
        "TotalPoints": -1,
        "PenaltyPoints": 3,
        "Person": "must not persist",
    }
    importer.apply(
        "pool_results",
        "10",
        {
            "MatchResult": [match_payload()],
            "PoolStanding": {"PoolStandingTeam": [row]},
            "ResultsFiltered": True,
        },
    )
    entry = PoolEntry.objects.get()
    assert entry.standing == {"Position": 1, "TotalPoints": -1, "PenaltyPoints": 3}
    assert Pool.objects.get().results_filtered
    assert Pool.objects.get().sport == "KORFBALL-VE-WK"


@pytest.mark.django_db
def test_bad_batch_is_atomic(season: Season) -> None:
    """A malformed row does not partially publish its preceding rows."""
    with pytest.raises(KeyError):
        Importer(season, timezone.now()).apply(
            "club_results", "CT1", {"MatchResult": [match_payload(), {}]}
        )
    assert not Match.objects.exists()
    assert not SyncResource.objects.exists()


@pytest.mark.django_db
def test_cancelled_result_is_removed_from_final_matches(season: Season) -> None:
    """A subsequent cancellation clears scores and retains revision history."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "CT1", {"MatchResult": [row]})
    row.update(Status="CANCELLED", HomeResult=None, AwayResult=None)
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    assert Match.objects.get().home_score is None
    assert Match.objects.get().status == "CANCELLED"
    assert list(ResultRevision.objects.values_list("status", flat=True)) == [
        "FINAL",
        "CANCELLED",
    ]
