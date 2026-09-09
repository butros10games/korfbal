"""Playing-time estimates and bounded reporting learning, without provider calls."""

from datetime import date, timedelta

from django.utils import timezone
import pytest

from apps.competition.domain.timing import expected_finish
from apps.competition.models import Match, Pool
from apps.competition.services.importer import Importer
from apps.competition.services.polling import (
    MINIMUM_REPORTING_SAMPLES,
    PollPlanner,
    next_result_check,
)
from apps.competition.tests.test_feed_coverage import seed
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


@pytest.mark.parametrize(
    ("context", "colour", "form", "discipline", "elapsed"),
    [
        (("b", "youth"), "blue", "four", "indoor", 70),
        (("b", "youth"), "red", "four", "outdoor", 70),
        (("b", "youth"), "orange", "eight", "indoor", 80),
        (("b", "youth"), "yellow", "eight", "outdoor", 80),
        (("b", "youth"), "red", "eight", "outdoor", 90),
        (("a", "U15"), "unknown", "eight", "indoor", 80),
        (("a", "U17"), "unknown", "eight", "outdoor", 90),
        (("a", "U17"), "unknown", "eight", "indoor", 90),
        (("unknown", "youth"), "unknown", "unknown", "indoor", 90),
    ],
)
def test_class_duration_and_stopped_clock_fallback(
    context: tuple[str, str], colour: str, form: str, discipline: str, elapsed: int
) -> None:
    """Shorter youth formats get earlier checks; unknown clock rules stay explicit."""
    category, age = context
    now = timezone.now()
    row = {
        "starts_at": now,
        "season__start_date": date(2026, 7, 1),
        "pool__competition_class__category": category,
        "pool__competition_class__age_group": age,
        "pool__competition_class__colour": colour,
        "pool__competition_class__playing_format": form,
        "pool__competition_class__edition__discipline": discipline,
    }
    assert expected_finish(row) == now + timedelta(minutes=elapsed)
    row["season__start_date"] = date(2024, 7, 1)
    assert expected_finish(row) == now + timedelta(minutes=90)
    row["playing_time_minutes"] = 50
    assert expected_finish(row) == now + timedelta(minutes=80)


@pytest.mark.django_db
def test_imported_minutes_survive_missing_invalid_and_stale_fields(
    season: Season,
) -> None:
    """Captured v8 fields are validated without dirtying publication timestamps."""
    now = timezone.now()
    row = match_payload()
    minutes = 50
    row.update(
        Duration=minutes,
        EventTimeResolution="MINUTE",
        MatchPeriod=[
            {"Description": "1e helft", "PlayTime": 25},
            {"Description": "2e helft", "PlayTime": 25},
        ],
    )
    Importer(season, now).apply("club_results", "CT1", {"MatchResult": [row]})
    fixture = Match.objects.get()
    assert fixture.playing_time_minutes == minutes
    updated_at = fixture.updated_at
    for invalid in (None, True, 0, -1, 3600, "2 x 25", "00:50", {}):
        row["Duration"] = invalid
        Importer(season, now + timedelta(seconds=1)).apply(
            "club_results", "CT1", {"MatchResult": [row]}
        )
        fixture.refresh_from_db()
        assert fixture.playing_time_minutes == minutes
        assert fixture.updated_at == updated_at
    row["Duration"] = 60
    Importer(season, now - timedelta(seconds=1)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    fixture.refresh_from_db()
    assert fixture.playing_time_minutes == minutes


@pytest.mark.parametrize("minutes", [40, 50, 60])
def test_first_check_tracks_imported_playing_time(minutes: int) -> None:
    """Give each unplayed fixture its own first deadline."""
    now = timezone.now()
    row = {
        "starts_at": now,
        "playing_time_minutes": minutes,
        "results_checked_at": None,
        "result_observed_at": None,
        "status": "SCHEDULED",
        "home_score": None,
        "away_score": None,
    }
    assert next_result_check(row, now) == now + timedelta(minutes=minutes + 30)


@pytest.mark.parametrize(
    ("gap", "rescheduled", "usable"),
    [(3, False, True), (30, False, False), (3, True, False)],
)
def test_learning_rejects_backlog_and_rescheduling(
    gap: int, rescheduled: bool, usable: bool
) -> None:
    """Reject delayed polling and moved kickoffs as reporting-rate evidence."""
    now = timezone.now()
    previous = {
        "reporting_delay_seconds": None,
        "status": "SCHEDULED",
        "home_score": None,
        "away_score": None,
        "result_observed_at": now - timedelta(minutes=gap),
        "starts_at": now - timedelta(hours=2),
    }
    result = {
        **previous,
        "status": "FINAL",
        "home_score": 0,
        "away_score": 1,
        "result_observed_at": now,
    }
    if rescheduled:
        result["starts_at"] = now
    assert PollPlanner._usable_reporting_sample(previous, result) is usable


@pytest.mark.django_db
@pytest.mark.parametrize("samples", [11, 12])
def test_learning_needs_evidence_and_caps_additional_wait(
    season: Season, samples: int
) -> None:
    """Twelve closely observed finals enable class learning, capped at 15 minutes."""
    seed(season, samples + 1)
    # Import an unambiguous youth class through normal classification.
    pool = Importer(season, timezone.now()).pool(
        {"PoolId": "0", "ClassName": "Rood"}, "KORFBALL-VE-BK"
    )
    pool.refresh_from_db()
    assert pool.competition_class_id is not None
    Pool.objects.update(
        competition_class_id=pool.competition_class_id, mapping_status="mapped"
    )
    Match.objects.exclude(external_id=f"M{samples}").update(
        status="FINAL", home_score=1, away_score=0, reporting_delay_seconds=3600
    )
    planner = PollPlanner(season, timezone.now())
    pending = next(row for row in planner.rows if row["status"] == "SCHEDULED")
    expected = 900 if samples == MINIMUM_REPORTING_SAMPLES else 0
    assert pending.get("learned_delay_seconds", 0) == expected


@pytest.mark.django_db
def test_first_final_collects_sample_without_dirtying_match(season: Season) -> None:
    """Store reporting evidence without changing publication timestamps."""
    rows = seed(season, 1)
    now = timezone.now()
    Match.objects.update(result_observed_at=now - timedelta(minutes=2))
    planner = PollPlanner(season, now)
    rows[0].update(Status="FINAL", HomeResult={"Score": 0}, AwayResult={"Score": 1})
    Importer(season, now).apply("club_results", "H0", {"MatchResult": rows})
    fixture = Match.objects.get()
    changed_at = fixture.updated_at
    assert planner.result_metrics()["reporting_samples_added"] == 1
    fixture.refresh_from_db()
    assert fixture.reporting_delay_seconds is not None
    assert fixture.updated_at == changed_at


@pytest.mark.parametrize(
    "field", ["playing_time_minutes", "pool__competition_class_id"]
)
def test_changed_timing_context_cannot_train_reporting_delay(field: str) -> None:
    """A changed duration or class is not evidence of provider reporting latency."""
    now = timezone.now()
    previous = {
        "reporting_delay_seconds": None,
        "status": "SCHEDULED",
        "home_score": None,
        "away_score": None,
        "result_observed_at": now - timedelta(minutes=2),
        "starts_at": now - timedelta(hours=2),
        "playing_time_minutes": 50,
        "pool__competition_class_id": 1,
    }
    result = {
        **previous,
        "status": "FINAL",
        "home_score": 1,
        "away_score": 0,
        "result_observed_at": now,
        field: 60,
    }
    assert not PollPlanner._usable_reporting_sample(previous, result)
