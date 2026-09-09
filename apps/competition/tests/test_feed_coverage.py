"""Request-count and response-coverage contracts using synthetic collections."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch

from django.utils import timezone
import pytest

from apps.competition.application.ports import FetchResult
from apps.competition.models import Match, Pool, SyncResource
from apps.competition.services.importer import Importer, enqueue
from apps.competition.services.polling import PollJob, PollPlanner
from apps.competition.services.sync import checkpoint, preview_sync, sync
from apps.competition.services.traffic import TrafficGate
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


def seed(season: Season, count: int = 12) -> list[dict]:
    """Four home clubs each host three different pools against distinct visitors."""
    now = timezone.now()
    rows = []
    for index in range(count):
        row = deepcopy(match_payload())
        row.update(
            PublicMatchId=f"M{index}",
            MatchDateTime=(now - timedelta(minutes=90)).isoformat(),
            Status="SCHEDULED",
            HomeResult=None,
            AwayResult=None,
            Pool={"PoolId": str(index), "PoolName": f"Pool {index}"},
        )
        for side, club in (("HomeTeam", f"H{index // 3}"), ("AwayTeam", f"A{index}")):
            row[side]["PublicTeamId"] = f"{side}{index}"
            row[side]["Club"]["ClubId"] = club
        rows.append(row)
    Importer(season, now - timedelta(hours=1)).apply(
        "club_results", "H0", {"MatchResult": rows}
    )
    Match.objects.update(
        playing_time_minutes=60,
        playing_time_observed_at=now,
        match_periods=[
            {"Description": "1e helft", "PlayTime": 30},
            {"Description": "2e helft", "PlayTime": 30},
        ],
        facility_observed_at=now,
        rules_observed_at=now,
    )
    enqueue(season, "clubs")
    Pool.objects.update(results_filtered=False)
    SyncResource.objects.update(
        fetched_at=now - timedelta(hours=1), next_sync_at=now + timedelta(days=1)
    )
    SyncResource.objects.filter(kind="club_program").update(fetched_at=now)
    return rows


@pytest.mark.django_db
def test_four_club_requests_replace_twelve_pool_checks(season: Season) -> None:
    """Assert end-to-end coverage and actual HTTP reservations, not planner scores."""
    rows = seed(season)
    client = Mock()
    preview = preview_sync(season)
    clubs = {row["HomeTeam"]["Club"]["ClubId"] for row in rows}
    assert preview["due_match_feed_estimate"] == len(clubs)
    assert preview["due_results"] == len(rows)

    def fetch(resource: SyncResource, gate: TrafficGate) -> FetchResult:
        gate.before_request()
        assert resource.kind == "club_results"
        return FetchResult(
            200,
            {
                "MatchResult": [
                    row
                    for row in rows
                    if row["HomeTeam"]["Club"]["ClubId"] == resource.source_id
                ]
            },
        )

    client.fetch.side_effect = fetch
    with patch("apps.competition.services.traffic.time.sleep"):
        summary = sync(season, client, budget=30)
    assert summary["http_requests"] == len({
        row["HomeTeam"]["Club"]["ClubId"] for row in rows
    })
    assert summary["matches_checked"] == len(rows)
    assert Match.objects.filter(results_checked_at__isnull=False).count() == len(rows)


@pytest.mark.django_db
@pytest.mark.parametrize("unchanged", [False, True])
def test_partial_response_leaves_absent_matches_due(
    season: Season, unchanged: bool
) -> None:
    """Neither HTTP 200 nor 304 proves coverage of an absent fixture."""
    rows = seed(season, 3)
    planner = PollPlanner(season, timezone.now())
    job = planner.next_job()
    assert job is not None
    assert job.resource.kind == "club_results"
    if unchanged:
        job.resource.match_ids = [Match.objects.get(external_id="M0").pk]
    result = (
        FetchResult(304) if unchanged else FetchResult(200, {"MatchResult": rows[:1]})
    )
    checked = checkpoint(job.resource, result, job)
    planner.completed(job, checked=checked)
    assert planner.checked == {Match.objects.get(external_id="M0").pk}
    remaining = planner.next_job()
    assert remaining is not None
    assert remaining.matches <= set(
        Match.objects.exclude(external_id="M0").values_list("pk", flat=True)
    )


@pytest.mark.django_db
def test_program_coverage_survives_next_worker_turn(season: Season) -> None:
    """One observed program suppresses opponent urgency, while daily audits remain."""
    rows = seed(season, 1)
    now = timezone.now()
    rows[0]["MatchDateTime"] = (now + timedelta(hours=2)).isoformat()
    Match.objects.update(starts_at=now + timedelta(hours=2))
    SyncResource.objects.filter(kind="club_program").update(
        fetched_at=now - timedelta(hours=1)
    )
    planner = PollPlanner(season, now)
    job = planner.next_job()
    assert job is not None
    assert job.resource.kind == "club_program"
    checked = checkpoint(
        job.resource,
        FetchResult(200, {"ProgramItemMatchClub": [{"Match": rows[0]}]}),
        job,
    )
    planner.completed(job, checked=checked)
    assert planner.next_job() is None
    assert PollPlanner(season, timezone.now()).next_job() is None
    opponent = (
        SyncResource.objects
        .filter(kind="club_program")
        .exclude(pk=job.resource.pk)
        .get()
    )
    opponent.next_sync_at = now - timedelta(seconds=1)
    opponent.save()
    audit = PollPlanner(season, timezone.now()).next_job()
    assert audit is not None
    assert audit.resource.pk == opponent.pk


@pytest.mark.django_db
def test_program_move_prevents_stale_result_check_in_same_run(season: Season) -> None:
    """A just-observed future kickoff immediately replaces the planner snapshot."""
    rows = seed(season, 1)
    planner = PollPlanner(season, timezone.now())
    program = SyncResource.objects.get(
        season=season, kind="club_program", source_id="H0"
    )
    job = PollJob(program, set(), 1)
    rows[0]["MatchDateTime"] = (timezone.now() + timedelta(days=3)).isoformat()
    checked = checkpoint(
        program, FetchResult(200, {"ProgramItemMatchClub": [{"Match": rows[0]}]}), job
    )
    planner.completed(job, checked=checked)
    assert planner.next_job() is None


@pytest.mark.django_db
def test_overdue_audit_receives_reserved_slot(season: Season) -> None:
    """Heavy current results cannot permanently starve catalogue discovery."""
    seed(season)
    now = timezone.now()
    audit = SyncResource.objects.get(season=season, kind="clubs")
    audit.next_sync_at = now - timedelta(hours=7)
    audit.save()
    planner = PollPlanner(season, now)
    for _ in range(4):
        job = planner.next_job()
        assert job is not None
        assert job.resource.kind == "club_results"
    fifth = planner.next_job()
    assert fifth is not None
    assert fifth.resource.pk == audit.pk


@pytest.mark.django_db
def test_recent_results_outrank_equal_coverage_with_old_backlog(season: Season) -> None:
    """Covering old fixtures cannot displace a feed covering more recent finishes."""
    seed(season, 6)
    now = timezone.now()
    Match.objects.filter(external_id__in=("M1", "M2")).update(
        starts_at=now - timedelta(days=4),
        results_checked_at=now - timedelta(days=2),
        result_observed_at=now - timedelta(days=2),
    )
    job = PollPlanner(season, now).next_job()
    assert job is not None
    assert job.resource.kind == "club_results"
    assert job.resource.source_id == "H1"


@pytest.mark.django_db
@pytest.mark.parametrize("stale", [False, True])
def test_recent_partial_membership_guides_feed_choice_without_hiding_matches(
    season: Season, stale: bool
) -> None:
    """Prefer demonstrated coverage, but let old scope be rediscovered."""
    seed(season, 6)
    now = timezone.now()
    matches = set(
        Match.objects.filter(external_id__in=("M0", "M1", "M2")).values_list(
            "pk", flat=True
        )
    )
    home = SyncResource.objects.get(kind="club_results", source_id="H0")
    home.match_ids = [min(matches)]
    home.fetched_at = now - timedelta(days=2) if stale else now - timedelta(hours=1)
    home.save()
    planner = PollPlanner(season, now)
    jobs = planner.candidate_jobs()
    home_job = next(job for job in jobs if job.resource.pk == home.pk)
    assert home_job.matches == matches
    selected = planner.next_job()
    assert selected is not None
    assert selected.resource.source_id == ("H0" if stale else "H1")
