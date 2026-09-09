"""Synthetic regression fixtures for provider identities and score corrections."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

from django.db import connection
from django.test.utils import CaptureQueriesContext
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
    entry = PoolEntry.objects.get(team__external_id="T1")
    assert PoolEntry.objects.get(team__external_id="T2").standing == {}
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


@pytest.mark.django_db
def test_repeated_standings_skip_membership_writes_and_clear_missing_rows(
    season: Season,
) -> None:
    """Stable polling skips membership writes while removed standings are cleared."""
    now = timezone.now()
    importer = Importer(season, now)
    importer.pool({"PoolId": 10})
    rows = [{**team_payload(f"T{index}"), "Position": index} for index in range(1, 7)]
    payload = {
        "MatchResult": [],
        "PoolStanding": {"PoolStandingTeam": rows},
        "ResultsFiltered": False,
    }
    importer.apply("pool_results", "10", payload)
    with CaptureQueriesContext(connection) as queries:
        Importer(season, now + timedelta(seconds=1)).apply(
            "pool_results", "10", payload
        )
    membership_writes = [
        query["sql"]
        for query in queries
        if "competition_poolentry" in query["sql"]
        and query["sql"].split()[0] in {"INSERT", "UPDATE", "DELETE"}
    ]
    assert membership_writes == []
    payload["PoolStanding"] = {"PoolStandingTeam": [{**rows[0], "Position": 2}]}
    Importer(season, now + timedelta(seconds=2)).apply("pool_results", "10", payload)
    entries = {
        entry.team.external_id: entry.standing
        for entry in PoolEntry.objects.select_related("team")
    }
    assert entries == {
        f"T{index}": ({"Position": 2} if index == 1 else {}) for index in range(1, 7)
    }


@pytest.mark.django_db
@pytest.mark.parametrize("result", [False, True])
def test_unchanged_import_only_writes_result_freshness(
    season: Season,
    result: bool,
) -> None:
    """Polling stable snapshots must not dirty publication or rewrite identities."""
    now = timezone.now()
    row = match_payload()
    kind = "club_results" if result else "club_program"
    payload = (
        {"MatchResult": [row]} if result else {"ProgramItemMatchClub": [{"Match": row}]}
    )
    Importer(season, now).apply(kind, "CT1", payload)
    before = Match.objects.get()
    checkpoint = SyncResource.objects.values_list("pk", "next_sync_at", "fetched_at")
    checkpoints = list(checkpoint)
    with CaptureQueriesContext(connection) as queries:
        Importer(season, now + timedelta(seconds=1)).apply(kind, "CT1", payload)
    writes = [
        query["sql"]
        for query in queries
        if query["sql"].split()[0] in {"INSERT", "UPDATE", "DELETE"}
    ]
    assert len(writes) == int(result)
    after = Match.objects.get()
    assert after.updated_at == before.updated_at
    assert list(checkpoint) == checkpoints
    if result:
        assert after.result_observed_at == now + timedelta(seconds=1)
        assert after.results_checked_at == after.result_observed_at
        assert after.revisions.count() == 1


@pytest.mark.django_db
def test_catalogue_changes_preserve_identity_and_nonblank_pool_metadata(
    season: Season,
) -> None:
    """Changed fields still persist without replacing reviewed identity links."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "CT1", {"MatchResult": [row]})
    team = Team.objects.get(external_id="T1")
    pool = Pool.objects.get()
    changed = deepcopy(row)
    changed["HomeTeam"]["TeamName"] = "Renamed team"
    changed["HomeTeam"]["Club"].update(ClubName="Renamed club", City="New city")
    changed["Pool"] = {"PoolId": 10, "PoolName": "Renamed pool"}
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_results", "CT1", {"MatchResult": [changed]}
    )
    updated_team = Team.objects.get(external_id="T1")
    assert (updated_team.pk, updated_team.group_id) == (team.pk, team.group_id)
    assert updated_team.name == "Renamed team"
    assert updated_team.club.name == "Renamed club"
    assert updated_team.club.city == "New city"
    updated_pool = Pool.objects.get()
    assert updated_pool.pk == pool.pk
    assert updated_pool.name == "Renamed pool"
    assert updated_pool.class_name == pool.class_name
    assert updated_pool.sport == pool.sport


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["SCHEDULED", "POSTPONED", "CANCELLED"])
def test_program_reschedules_unscored_result_observation(
    season: Season, status: str
) -> None:
    """A result-feed placeholder must not freeze later program changes."""
    now = timezone.now()
    row = match_payload()
    row.update(Status=status, HomeResult=None, AwayResult=None)
    Importer(season, now).apply("club_results", "CT1", {"MatchResult": [row]})
    original = Match.objects.get()
    row.update(Status="SCHEDULED", MatchDateTime="2026-09-12T15:00:00+0200")
    program = {"ProgramItemMatchClub": [{"Match": row}]}
    Importer(season, now - timedelta(seconds=1)).apply("club_program", "CT1", program)
    assert Match.objects.get().starts_at == original.starts_at
    Importer(season, now + timedelta(seconds=1)).apply("club_program", "CT1", program)
    moved = Match.objects.get()
    assert moved.starts_at.isoformat() == "2026-09-12T13:00:00+00:00"
    assert moved.status == "SCHEDULED"
    assert moved.home_score is None
    assert moved.result_observed_at == original.result_observed_at
    assert moved.revisions.count() == 1
    Importer(season, now + timedelta(seconds=2)).apply("club_program", "CT1", program)
    assert Match.objects.get().updated_at == moved.updated_at
    row.update(Status="FINAL", HomeResult={"Score": 0}, AwayResult={"Score": 12})
    Importer(season, now + timedelta(days=7)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    finished = Match.objects.get()
    assert (finished.status, finished.home_score, finished.away_score) == (
        "FINAL",
        0,
        12,
    )
    assert finished.starts_at == moved.starts_at
    assert list(finished.revisions.values_list("status", flat=True)) == [
        status,
        "FINAL",
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(("status", "score"), [("FINAL", None), ("SCHEDULED", 0)])
def test_program_preserves_finished_or_scored_matches(
    season: Season, status: str, score: int | None
) -> None:
    """Scoreless finals and partial scores also outrank program summaries."""
    now = timezone.now()
    row = match_payload()
    row.update(Status=status, HomeResult={"Score": score}, AwayResult=None)
    Importer(season, now).apply("club_results", "CT1", {"MatchResult": [row]})
    original = Match.objects.get()
    row.update(Status="SCHEDULED", MatchDateTime="2026-09-12T15:00:00+0200")
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_program", "CT1", {"ProgramItemMatchClub": [{"Match": row}]}
    )
    assert Match.objects.get().starts_at == original.starts_at
    assert Match.objects.get().updated_at == original.updated_at
