"""Edition, competition-period and participation regressions (season audit)."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Any

from django.utils import timezone
import pytest

from apps.competition.domain.classification import classify, designation
from apps.competition.domain.competition_periods import (
    AUTUMN,
    FULL_SEASON,
    SPRING,
    indoor_parts,
    outdoor_phase,
)
from apps.competition.models import (
    HistoricalResource,
    Match as SourceMatch,
    Pool,
    SeasonBinding,
    Team as SourceTeam,
    TeamParticipation,
)
from apps.competition.services.classification import map_pool, plan_pool
from apps.competition.services.context_repair import RepairOptions, run
from apps.competition.services.history import resource_key
from apps.competition.services.history_editions import import_rows, prepare_edition
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.seasons import (
    INDOOR,
    OUTDOOR,
    configure_seasons,
    native_match_filter,
)
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload, team_payload
from apps.competition.tests.test_rosters import person
from apps.player.models import Player
from apps.schedule.domain.competition_context import INDOOR_PHASE
from apps.schedule.models import Match, Season
from apps.schedule.queries.seasons import season_edition, season_kind
from apps.schedule.tests.season_builders import playing_season
from apps.team.models import TeamData


pytestmark = pytest.mark.django_db


def fixture(
    identifier: str, when: str, pool: int, *, home: str = "T1", away: str = "T2"
) -> dict[str, Any]:
    """Return one fabricated outdoor fixture of a poule."""
    row = deepcopy(match_payload())
    row.update(PublicMatchId=identifier, MatchDateTime=when, Status="SCHEDULED")
    row["HomeTeam"] = team_payload(home)
    row["AwayTeam"] = team_payload(away)
    row["Pool"] = {"PoolId": pool, "PoolName": f"P{pool}", "ClassName": "1e klasse"}
    row.pop("HomeResult")
    row.pop("AwayResult")
    return row


def annual_scope() -> Season:
    """Create a provider import scope for one live edition."""
    return Season.objects.create(
        name="Sportlink 2026-2027",
        start_date=date(2026, 7, 1),
        end_date=date(2027, 6, 30),
    )


def publish() -> dict[str, Any]:
    """Run one publication pass."""
    return publish_catalogue(schedule_changes=RecordingScheduleChanges())


# Annual edition selection ------------------------------------------------------


@pytest.mark.parametrize(
    ("calendar_year", "label", "issue"),
    [
        # Spring 2025 belongs to 2024-2025: legacy A-F youth classes are valid.
        (2025, "gemengd A-jeugd 1e klasse", "age_scheme_season_conflict"),
        # Spring 2026 belongs to 2025-2026: 2026-2027 removals do not apply yet.
        (2026, "gemengd U19 2e klasse", "class_removed_for_season"),
    ],
)
def test_spring_classification_uses_its_edition(
    calendar_year: int, label: str, issue: str
) -> None:
    """A January-June season selects the rules of the previous July's edition."""
    spring = playing_season(SPRING, calendar_year - 1)
    pool = Pool.objects.create(
        season=spring, external_id="SPRING", class_name=label, sport=OUTDOOR
    )
    assert season_edition(spring) == calendar_year - 1
    assert issue not in plan_pool(pool)["issues"]


def test_edition_2026_applies_its_class_removals() -> None:
    """The 2026-2027 edition removes the mixed U19 second class."""
    autumn = playing_season(AUTUMN, 2026)
    spring = playing_season(SPRING, 2026)
    for season in (autumn, spring):
        pool = Pool.objects.create(
            season=season,
            external_id=f"U19-{season.pk}",
            class_name="gemengd U19 2e klasse",
            sport=OUTDOOR,
        )
        assert "class_removed_for_season" in plan_pool(pool)["issues"]


def test_indoor_season_across_new_year_keeps_its_edition() -> None:
    """An indoor season running October-March belongs to the edition it starts."""
    edition = 2024
    indoor = playing_season(INDOOR_PHASE, edition)
    pool = Pool.objects.create(
        season=indoor, external_id="Z", class_name="A-jeugd 1e klasse", sport=INDOOR
    )
    assert season_edition(indoor) == edition
    assert "age_scheme_season_conflict" not in plan_pool(pool)["issues"]


def test_ambiguous_legacy_season_stays_unresolved() -> None:
    """A season crossing July has no inferable edition and gets no class."""
    legacy = Season.objects.create(
        name="Oude import", start_date=date(2019, 3, 1), end_date=date(2019, 9, 30)
    )
    pool = Pool.objects.create(
        season=legacy, external_id="OLD", class_name="1e klasse", sport=OUTDOOR
    )
    decision = map_pool(pool)
    pool.refresh_from_db()
    assert season_edition(legacy) is None
    assert decision["status"] == "unresolved"
    assert "unresolved_edition" in decision["issues"]
    assert pool.competition_class_id is None
    assert designation("Example U19-1", None)["kind"] == "unknown"


def test_designation_follows_the_edition_not_the_calendar_year() -> None:
    """A spring 2025 team name still uses the 2024-2025 historical scheme."""
    assert designation("Example A1", 2024)["kind"] == "historical"
    assert designation("Example U19-1", 2024)["kind"] == "unknown"
    assert (
        classify("A-jeugd 1e klasse", OUTDOOR, 2024)[1].count(
            "age_scheme_season_conflict"
        )
        == 0
    )


# Renamed seasons ---------------------------------------------------------------


def test_renaming_a_season_never_changes_its_context() -> None:
    """Kind, phase and edition are stored, not parsed from the display name."""
    autumn = playing_season(AUTUMN, 2025)
    renamed = playing_season(SPRING, 2025)
    Season.objects.filter(pk=autumn.pk).update(name="Na seizoen 2026 (oud)")
    Season.objects.filter(pk=renamed.pk).update(name="Voor seizoen 2025 (oud)")
    autumn.refresh_from_db()
    renamed.refresh_from_db()
    assert season_kind(autumn) == "autumn"
    assert season_kind(renamed) == "spring"
    unnamed = Season.objects.create(
        name="Voor seizoen 2030",
        start_date=date(2030, 7, 1),
        end_date=date(2030, 12, 31),
    )
    # A canonical-looking name on a new, unrecorded season decides nothing.
    assert unnamed.context.phase is None
    assert unnamed.context.edition == unnamed.start_date.year


# Live and historical period routing -------------------------------------------


def test_live_split_routes_independent_and_continuous_poules() -> None:
    """Autumn, spring and continuous poules publish into their own seasons."""
    scope = annual_scope()
    configure_seasons(scope, 2026, split_outdoor=True)
    rows = [
        fixture("A1", "2026-09-05T13:30:00+0200", 10),
        fixture("A2", "2026-10-03T13:30:00+0200", 10, home="T2", away="T1"),
        fixture("S1", "2027-04-03T13:30:00+0200", 20),
        fixture("S2", "2027-05-08T13:30:00+0200", 20, home="T2", away="T1"),
        fixture("C1", "2026-09-12T13:30:00+0200", 30, home="T3", away="T4"),
        fixture("C2", "2026-10-10T13:30:00+0200", 30, home="T4", away="T3"),
        fixture("C3", "2027-04-10T13:30:00+0200", 30, home="T3", away="T4"),
        fixture("C4", "2027-05-15T13:30:00+0200", 30, home="T4", away="T3"),
    ]
    Importer(scope, timezone.now()).apply("club_results", "CT1", {"MatchResult": rows})
    publish()
    seasons = {
        row.external_id: row.local_match.season
        for row in SourceMatch.objects.select_related("local_match__season")
    }
    assert {seasons["A1"].phase, seasons["A2"].phase} == {AUTUMN}
    assert {seasons["S1"].phase, seasons["S2"].phase} == {SPRING}
    assert {seasons[key].pk for key in ("C1", "C2", "C3", "C4")} == {scope.pk}
    assert dict(Pool.objects.values_list("external_id", "phase")) == {
        "10": AUTUMN,
        "20": SPRING,
        "30": FULL_SEASON,
    }
    # One source team plays two independent periods with separate rosters.
    team = SourceTeam.objects.get(external_id="T1")
    periods = dict(
        TeamParticipation.objects.filter(team=team).values_list(
            "phase", "team_data__season__phase"
        )
    )
    assert periods == {AUTUMN: AUTUMN, SPRING: SPRING}
    # Source rows keep the provider scope; native filters follow the routing.
    assert set(SourceMatch.objects.values_list("season_id", flat=True)) == {scope.pk}
    autumn = seasons["A1"]
    assert set(
        SourceMatch.objects.filter(native_match_filter(autumn.pk)).values_list(
            "external_id", flat=True
        )
    ) == {"A1", "A2"}


def test_unsplit_scope_keeps_one_outdoor_season() -> None:
    """Existing configured scopes keep publishing every outdoor poule together."""
    scope = annual_scope()
    configure_seasons(scope, 2026)
    rows = [
        fixture("A1", "2026-09-05T13:30:00+0200", 10),
        fixture("S1", "2027-04-03T13:30:00+0200", 20),
    ]
    Importer(scope, timezone.now()).apply("club_results", "CT1", {"MatchResult": rows})
    publish()
    assert set(Match.objects.values_list("season_id", flat=True)) == {scope.pk}
    assert not SeasonBinding.objects.exclude(phase="").exists()
    assert not TeamParticipation.objects.exists()


def test_rescheduled_fixture_stays_in_its_competition() -> None:
    """A fixture moved across the winter break keeps its poule's season."""
    scope = annual_scope()
    configure_seasons(scope, 2026, split_outdoor=True)
    rows = [
        fixture(f"A{day}", f"2026-09-{day:02d}T13:30:00+0200", 10)
        for day in (5, 12, 19, 26)
    ]
    rows.append(fixture("LATE", "2027-01-16T13:30:00+0200", 10))
    Importer(scope, timezone.now()).apply("club_results", "CT1", {"MatchResult": rows})
    publish()
    late = SourceMatch.objects.select_related("local_match__season").get(
        external_id="LATE"
    )
    assert late.local_match.season.phase == AUTUMN
    # A later reschedule of a published fixture never moves it either.
    moved = fixture("A5", "2027-03-20T13:30:00+0200", 10)
    Importer(scope, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [moved]}
    )
    publish()
    pool = Pool.objects.get(external_id="10")
    assert pool.phase == AUTUMN
    assert SourceMatch.objects.get(external_id="A5").local_match.season.phase == AUTUMN


def test_published_period_change_waits_for_review() -> None:
    """Spring fixtures of an autumn-published poule wait; the repair corrects it."""
    scope = annual_scope()
    configure_seasons(scope, 2026, split_outdoor=True)
    rows = [
        fixture("A1", "2026-09-05T13:30:00+0200", 10),
        fixture("A2", "2026-09-19T13:30:00+0200", 10),
    ]
    Importer(scope, timezone.now()).apply("club_results", "CT1", {"MatchResult": rows})
    publish()
    spring = [
        fixture(f"S{day}", f"2027-04-{day:02d}T13:30:00+0200", 10)
        for day in (3, 10, 17)
    ]
    Importer(scope, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": spring}
    )
    result = publish()
    pool = Pool.objects.get(external_id="10")
    assert pool.phase == AUTUMN
    assert pool.phase_evidence["review"]["observed"] == FULL_SEASON
    assert result["counts"]["pool_periods_conflicts"] >= 1
    assert set(Match.objects.values_list("season__phase", flat=True).distinct()) == {
        AUTUMN
    }
    # Spring fixtures are held back instead of being published into autumn.
    assert result["counts"]["matches_period_review"] == len(spring)
    assert Match.objects.count() == len(rows)
    native_ids = set(Match.objects.values_list("pk", flat=True))

    preview = run(RepairOptions(scope=scope, split_outdoor=True))
    assert preview.counts["period_corrections"] == 1
    assert Pool.objects.get(pk=pool.pk).phase == AUTUMN

    run(RepairOptions(scope=scope, apply=True, split_outdoor=True))
    pool.refresh_from_db()
    assert pool.phase == FULL_SEASON
    assert "review" not in pool.phase_evidence
    assert pool.phase_evidence["corrected_from"] == AUTUMN
    assert set(Match.objects.values_list("season_id", flat=True)) == {scope.pk}
    assert set(Match.objects.values_list("pk", flat=True)) == native_ids
    assert not TeamParticipation.objects.filter(phase=AUTUMN).exists()

    publish()
    assert Match.objects.count() == len(rows) + len(spring)
    assert set(Match.objects.values_list("season_id", flat=True)) == {scope.pk}


def test_historical_poule_follows_all_fixtures_not_one_date() -> None:
    """History uses the same rule: one rescheduled January fixture stays autumn."""
    seasons = prepare_edition(2024)
    rows = [
        {
            **fixture(f"H{day}", f"2024-09-{day:02d}T13:30:00+0200", 77),
            "Status": "FINAL",
        }
        for day in (7, 14, 21, 28)
    ]
    rows.append({**fixture("HJAN", "2025-01-18T13:30:00+0200", 77), "Status": "FINAL"})
    anchor = seasons.indoor
    resource = HistoricalResource.objects.create(
        key=resource_key(
            anchor, "app", "edition_pool", "77", (anchor.start_date, anchor.end_date)
        ),
        season=anchor,
        provider="app",
        kind="edition_pool",
        source_id="77",
        start_date=anchor.start_date,
        end_date=anchor.end_date,
    )
    summary = import_rows(seasons, resource, rows)
    assert summary["skipped"] == {}
    assert set(SourceMatch.objects.values_list("season_id", flat=True)) == {
        seasons.autumn.pk
    }
    assert Pool.objects.get(season=seasons.autumn, external_id="77").phase == AUTUMN


def test_one_team_roster_follows_the_period_running_when_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Autumn and spring rosters stay separate; manual players are preserved."""
    scope = annual_scope()
    configure_seasons(scope, 2026, split_outdoor=True)
    rows = [
        fixture("A1", "2026-09-05T13:30:00+0200", 10),
        fixture("S1", "2027-04-03T13:30:00+0200", 20),
    ]
    Importer(scope, timezone.now()).apply("club_results", "CT1", {"MatchResult": rows})
    publish()
    team = SourceTeam.objects.get(external_id="T1")
    autumn = TeamParticipation.objects.get(team=team, phase=AUTUMN).team_data
    spring = TeamParticipation.objects.get(team=team, phase=SPRING).team_data
    manual = Player.objects.create(name="Handmatig toegevoegd")
    autumn.players.add(manual)

    def observe(moment: datetime, people: list[str]) -> None:
        monkeypatch.setattr(timezone, "now", lambda: moment)
        Importer(scope, moment).apply(
            "team_roster",
            "T1",
            {"TeamPersonOverview": [person(identifier) for identifier in people]},
        )

    observe(datetime(2026, 9, 1, 12, tzinfo=UTC), ["P-AUTUMN"])
    assert set(autumn.players.values_list("knkv_person_id", flat=True)) == {
        "P-AUTUMN",
        None,
    }
    observe(datetime(2027, 3, 20, 12, tzinfo=UTC), ["P-SPRING"])

    def linked(team_data: TeamData) -> set[str | None]:
        # Stored links: stale source-only people are hidden from Player.objects.
        return set(
            Player.all_objects.filter(
                pk__in=TeamData.players.through.objects.filter(
                    teamdata_id=team_data.pk
                ).values("player_id")
            ).values_list("knkv_person_id", flat=True)
        )

    assert linked(autumn) == {"P-AUTUMN", None}
    assert linked(spring) == {"P-SPRING"}
    assert {autumn.season_id, spring.season_id} <= set(
        TeamData.objects.filter(team=autumn.team).values_list("season_id", flat=True)
    )


# Shared period rules -----------------------------------------------------------


def test_outdoor_phase_rules() -> None:
    """Whole-poule evidence decides; a single odd fixture is a reschedule."""
    sept, jan, april = date(2026, 9, 5), date(2027, 1, 16), date(2027, 4, 3)
    assert outdoor_phase([sept, sept], 2026).phase == AUTUMN
    assert outdoor_phase([april], 2026).phase == SPRING
    assert outdoor_phase([sept, sept, april, april], 2026).phase == FULL_SEASON
    assert outdoor_phase([sept] * 4 + [jan], 2026).phase == AUTUMN
    assert outdoor_phase([sept, april], 2026).phase is None
    assert outdoor_phase([april, april], 2026, label_phase=AUTUMN).phase == AUTUMN


def test_indoor_parts_follow_team_sequences_not_new_year() -> None:
    """Parts split in mid-January; a team's consecutive poules are numbered."""
    first = ("P1", date(2026, 11, 7), date(2027, 1, 10))
    second = ("P2", date(2027, 1, 23), date(2027, 3, 13))
    decisions = indoor_parts([[second, first], [first, second]])
    assert decisions["P1"].evidence["part"] == 1
    assert decisions["P2"].evidence["part"] == len([first, second])
    overlapping = indoor_parts([[first, ("P3", date(2026, 12, 1), date(2027, 2, 1))]])
    assert overlapping["P1"].evidence["part"] is None
