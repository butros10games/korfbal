"""Official timing and edition rules reach the native tracker (season audit)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.utils import timezone
import pytest

from apps.competition.domain.match_rules import RuleContext, resolve_rules
from apps.competition.models import Match as SourceMatch
from apps.competition.services.context_repair import RepairOptions, run
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload
from apps.game_tracker.domain.match_rules import EDITION_RULES, OFFICIAL_TIMING
from apps.game_tracker.models import MatchData
from apps.schedule.models import Season


pytestmark = pytest.mark.django_db


def scope() -> Season:
    """Create a 2026-2027 provider scope."""
    return Season.objects.create(
        name="Rules 2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )


def timed(
    periods: list[tuple[str, int]] | None, *, class_name: str = "Example class"
) -> dict[str, Any]:
    """Return a fixture, optionally with official minutes per period."""
    row = match_payload()
    row["Pool"]["ClassName"] = class_name
    if periods is not None:
        row.update(
            # Regulation minutes only: extra time is not part of the duration.
            Duration=sum(
                minutes
                for name, minutes in periods
                if "verlenging" not in name and name != "Strafworpserie"
            ),
            EventTimeResolution="MINUTE",
            MatchPeriod=[
                {"Description": name, "PlayTime": minutes} for name, minutes in periods
            ],
        )
    return row


def published_tracker(season: Season, row: dict[str, Any]) -> MatchData:
    """Import and publish one fixture, returning its tracker."""
    Importer(season, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = SourceMatch.objects.get(external_id=row["PublicMatchId"])
    return MatchData.objects.get(match_link_id=source.local_match_id)


@pytest.mark.parametrize(
    ("periods", "parts", "length"),
    [
        ([("1e helft", 25), ("2e helft", 25)], 2, 1500),
        ([(f"Periode {n}", 10) for n in range(1, 5)], 4, 600),
        ([("1e helft", 30), ("2e helft", 30)], 2, 1800),
    ],
)
def test_published_tracker_uses_official_periods(
    periods: list[tuple[str, int]], parts: int, length: int
) -> None:
    """2 x 25 is 50 minutes, 4 x 10 is four periods, 2 x 30 stays 60."""
    tracker = published_tracker(scope(), timed(periods))
    rules = tracker.match_rules()
    assert (tracker.parts, tracker.part_length) == (parts, length)
    assert rules.source == OFFICIAL_TIMING
    assert rules.duration_resolved
    assert rules.regulation_minutes == parts * length // 60


def test_extra_time_keeps_its_own_lengths() -> None:
    """Additional periods are recorded separately, never as regulation time."""
    periods = [
        ("1e helft", 25),
        ("2e helft", 25),
        ("1e verlenging", 5),
        ("2e verlenging", 5),
        ("Strafworpserie", 0),
    ]
    tracker = published_tracker(scope(), timed(periods))
    rules = tracker.match_rules()
    assert (tracker.parts, tracker.part_length) == (2, 1500)
    assert rules.periods == (25, 25)
    assert [(p.description, p.minutes) for p in rules.additional_periods] == [
        ("1e verlenging", 5),
        ("2e verlenging", 5),
        ("Strafworpserie", 0),
    ]


def test_verified_class_rules_apply_without_official_timing() -> None:
    """2026-2027 A senior outdoor: 2 x 30 from the handbook, eight substitutions."""
    tracker = published_tracker(
        scope(), timed(None, class_name="gemengd senioren 1e klasse")
    )
    rules = tracker.match_rules()
    assert rules.source == EDITION_RULES
    assert rules.periods == (30, 30)
    assert rules.effective_substitution_limit() == 8  # noqa: PLR2004 - KNKV 9.6.1
    assert rules.rule_version == "knkv-2026-2027"


def test_unverified_edition_keeps_rules_unresolved() -> None:
    """Earlier editions get no guessed duration; the clock stays an assumption."""
    season = Season.objects.create(
        name="Rules 2021-2022", start_date=date(2021, 7, 1), end_date=date(2022, 6, 30)
    )
    row = timed(None, class_name="gemengd senioren 1e klasse")
    row["MatchDateTime"] = "2021-09-04T13:30:00+0200"
    tracker = published_tracker(season, row)
    rules = tracker.match_rules()
    assert rules.periods is None
    assert not rules.duration_resolved
    assert "substitutions" in rules.unresolved
    assert (tracker.parts, tracker.part_length) == (2, 1800)


def test_invalid_or_stale_timing_cannot_replace_an_observation() -> None:
    """Rejected and older provider timing leaves source and tracker unchanged."""
    season = scope()
    tracker = published_tracker(season, timed([("1e helft", 25), ("2e helft", 25)]))
    invalid = timed([("1e helft", 30), ("2e helft", 25)])
    invalid.update(EventTimeResolution="NONE", Duration=60)
    Importer(season, timezone.now() + timedelta(hours=1)).apply(
        "club_results", "CT1", {"MatchResult": [invalid]}
    )
    stale = timed([("1e helft", 30), ("2e helft", 30)])
    Importer(season, timezone.now() - timedelta(days=1)).apply(
        "club_results", "CT1", {"MatchResult": [stale]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    tracker.refresh_from_db()
    assert SourceMatch.objects.get().playing_time_minutes == 50  # noqa: PLR2004
    assert (tracker.parts, tracker.part_length) == (2, 1500)


def test_late_timing_reconciles_a_pristine_match() -> None:
    """Timing observed after publication reaches an untouched tracker at once."""
    season = scope()
    tracker = published_tracker(season, timed(None))
    assert tracker.match_rules().periods is None
    updated_at = SourceMatch.objects.get().updated_at
    Importer(season, timezone.now() + timedelta(minutes=5)).apply(
        "club_results",
        "CT1",
        {"MatchResult": [timed([("1e helft", 25), ("2e helft", 25)])]},
    )
    tracker.refresh_from_db()
    assert (tracker.parts, tracker.part_length) == (2, 1500)
    # Timing metadata alone does not mark the fixture as changed content.
    assert SourceMatch.objects.get().updated_at == updated_at


def test_tracked_match_gets_a_reviewed_correction() -> None:
    """Active tracking is never rewritten; a reviewed repair applies it later."""
    season = scope()
    tracker = published_tracker(season, timed(None))
    MatchData.objects.filter(pk=tracker.pk).update(
        status="active", command_sequence=3, live_revision=3, score_source="tracker"
    )
    Importer(season, timezone.now() + timedelta(minutes=5)).apply(
        "club_results",
        "CT1",
        {"MatchResult": [timed([("1e helft", 25), ("2e helft", 25)])]},
    )
    tracker.refresh_from_db()
    assert (tracker.parts, tracker.part_length) == (2, 1800)
    assert tracker.rules_pending["rules"]["periods"] == [25, 25]

    preview = run(
        RepairOptions(scope=season, accept_reviewed=frozenset({tracker.match_link_id}))
    )
    assert preview.counts["reviewed_to_accept"] == 1
    tracker.refresh_from_db()
    assert tracker.part_length == 1800  # noqa: PLR2004 - preview wrote nothing

    changes: list[MatchData] = []
    report = run(
        RepairOptions(
            scope=season,
            apply=True,
            accept_reviewed=frozenset({tracker.match_link_id}),
            record_change=changes.append,
        )
    )
    tracker.refresh_from_db()
    assert report.counts["reviewed_accepted"] == 1
    assert (tracker.parts, tracker.part_length) == (2, 1500)
    assert tracker.rules_pending == {}
    assert [change.pk for change in changes] == [tracker.pk]


def test_u17_indoor_duration_does_not_reveal_the_clock() -> None:
    """KNKV 7.5.1: U17 plays 2 x 25 indoors with and without a shot clock."""
    indoor = RuleContext(
        edition=2026, discipline="indoor", category="a", age_group="U17"
    )
    official = resolve_rules(
        indoor,
        minutes=50,
        periods=[
            {"Description": "1e helft", "PlayTime": 25},
            {"Description": "2e helft", "PlayTime": 25},
        ],
    )
    assert official.periods == (25, 25)
    assert (official.clock, official.shot_clock) == (None, None)
    assert {"clock", "shot_clock"} <= set(official.unresolved)
    fallback = resolve_rules(indoor)
    assert fallback.periods == (25, 25)
    assert fallback.duration_resolved
    outdoor = resolve_rules(
        RuleContext(edition=2026, discipline="outdoor", category="a", age_group="U17")
    )
    assert outdoor.periods == (30, 30)
    assert outdoor.clock == "running"
    senior = resolve_rules(
        RuleContext(edition=2026, discipline="indoor", category="a", age_group="U19"),
        minutes=50,
        periods=[
            {"Description": "1e helft", "PlayTime": 25},
            {"Description": "2e helft", "PlayTime": 25},
        ],
    )
    assert (senior.clock, senior.shot_clock) == ("stopped", True)


@pytest.mark.parametrize(
    "case",
    [
        # KNKV 9.2: U17 uses a shot clock only in the indoor hoofdklasse.
        ("mixed", "U17", "youth", "class_1", (25, 25), ("running", False)),
        ("mixed", "U17", "youth", "hoofdklasse", (25, 25), ("stopped", True)),
        ("mixed", "U19", "youth", "overgangsklasse", (25, 25), ("stopped", True)),
        ("mixed", "U19", "youth", "class_1", (30, 30), ("running", False)),
        ("mixed", "senior", "standard", "class_1", (25, 25), ("stopped", True)),
        ("mixed", "senior", "standard", "class_2", (30, 30), ("running", False)),
        ("women", "U19", "youth", "hoofdklasse", (25, 25), ("stopped", True)),
        # Reserve teams: only the reserve classes 9.2 names use a shot clock.
        ("mixed", "senior", "reserve", "class_1", (30, 30), ("running", False)),
        ("mixed", "senior", "reserve", "hoofdklasse", (25, 25), ("stopped", True)),
        ("women", "senior", "reserve", "hoofdklasse", (30, 30), ("running", False)),
        ("women", "senior", "reserve", "topklasse", (25, 25), ("stopped", True)),
        ("women", "senior", "standard", "hoofdklasse", (25, 25), ("stopped", True)),
        # Without a team kind, a senior class is decided only when both agree.
        ("mixed", "senior", "unknown", "hoofdklasse", (25, 25), ("stopped", True)),
        ("mixed", "senior", "unknown", "class_2", (30, 30), ("running", False)),
    ],
)
def test_indoor_shot_clock_follows_the_class(
    case: tuple[str, str, str, str, tuple[int, int], tuple[str, bool]],
) -> None:
    """The official class decides the shot clock, and with it the clock type."""
    gender, age, kind, code, periods, clock = case
    rules = resolve_rules(
        RuleContext(
            edition=2026,
            discipline="indoor",
            category="a",
            age_group=age,
            code=code,
            gender=gender,
            team_kind=kind,
        )
    )
    assert rules.periods == periods
    assert (rules.clock, rules.shot_clock) == clock
    assert "clock" not in rules.unresolved


def test_contradicting_official_periods_leave_the_clock_unknown() -> None:
    """A U17 hoofdklasse match reported as 2 x 30 does not match its class."""
    rules = resolve_rules(
        RuleContext(
            edition=2026,
            discipline="indoor",
            category="a",
            age_group="U17",
            code="hoofdklasse",
            gender="mixed",
        ),
        minutes=60,
        periods=[
            {"Description": "1e helft", "PlayTime": 30},
            {"Description": "2e helft", "PlayTime": 30},
        ],
    )
    assert rules.periods == (30, 30)
    assert (rules.clock, rules.shot_clock) == (None, None)


def test_outdoor_never_uses_a_shot_clock() -> None:
    """KNKV 9.2: the whole outdoor competition plays without a shot clock."""
    rules = resolve_rules(
        RuleContext(
            edition=2026,
            discipline="outdoor",
            category="a",
            age_group="senior",
            code="hoofdklasse",
            gender="mixed",
        )
    )
    assert (rules.clock, rules.shot_clock) == ("running", False)


@pytest.mark.parametrize(
    ("gender", "code"), [("mixed", "class_1"), ("women", "hoofdklasse")]
)
def test_senior_class_without_team_kind_stays_unresolved(
    gender: str, code: str
) -> None:
    """Standard and reserve teams differ here, so no duration or clock is guessed."""
    rules = resolve_rules(
        RuleContext(
            edition=2026,
            discipline="indoor",
            category="a",
            age_group="senior",
            code=code,
            gender=gender,
        )
    )
    assert rules.periods is None
    assert (rules.clock, rules.shot_clock) == (None, None)
