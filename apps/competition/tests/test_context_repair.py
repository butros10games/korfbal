"""The competition-context repair previews by default and applies idempotently."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from typing import Any

from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.competition.models import (
    Match as SourceMatch,
    Pool,
    SeasonBinding,
    TeamParticipation,
)
from apps.competition.services.context_repair import RepairOptions, run
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.seasons import configure_seasons, native_match_filter
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload, team_payload
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match, Season, SeasonPool
from apps.schedule.tests.season_builders import playing_season


pytestmark = pytest.mark.django_db


def fixture(identifier: str, when: str, pool: int) -> dict[str, Any]:
    """Return one fabricated outdoor fixture."""
    row = deepcopy(match_payload())
    row.update(PublicMatchId=identifier, MatchDateTime=when, Status="SCHEDULED")
    row["HomeTeam"], row["AwayTeam"] = team_payload("T1"), team_payload("T2")
    row["Pool"] = {"PoolId": pool, "PoolName": f"P{pool}", "ClassName": "1e klasse"}
    row.pop("HomeResult")
    row.pop("AwayResult")
    return row


def legacy_scope() -> Season:
    """Publish an unsplit live scope holding one autumn and one spring poule."""
    scope = Season.objects.create(
        name="Repair 2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )
    configure_seasons(scope, 2026)
    rows = [
        fixture("A1", "2026-09-05T13:30:00+0200", 10),
        fixture("A2", "2026-09-19T13:30:00+0200", 10),
        fixture("S1", "2027-04-03T13:30:00+0200", 20),
        fixture("S2", "2027-04-17T13:30:00+0200", 20),
    ]
    Importer(scope, timezone.now()).apply("club_results", "CT1", {"MatchResult": rows})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    return scope


def snapshot() -> dict[str, Any]:
    """Capture every row the repair may write."""
    return {
        "matches": sorted(Match.objects.values_list("pk", "season_id")),
        "pools": sorted(Pool.objects.values_list("pk", "phase", "local_pool_id")),
        "bindings": sorted(SeasonBinding.objects.values_list("sport", "phase")),
        "participations": TeamParticipation.objects.count(),
        "rules": sorted(MatchData.objects.values_list("pk", "rules_source")),
    }


def test_preview_writes_nothing_and_reports_moves() -> None:
    """The default preview only reports planned fixture moves."""
    scope = legacy_scope()
    before = snapshot()
    report = run(RepairOptions(scope=scope, split_outdoor=True))
    assert snapshot() == before
    assert report.counts["fixtures_to_move"] == len(["A1", "A2", "S1", "S2"])
    assert not report.applied


def test_apply_moves_fixtures_and_is_idempotent() -> None:
    """Fixtures keep their UUIDs; a second run changes nothing."""
    scope = legacy_scope()
    native_ids = set(Match.objects.values_list("pk", flat=True))
    report = run(RepairOptions(scope=scope, apply=True, split_outdoor=True))
    assert report.counts["fixtures_moved"] == len(native_ids)
    assert set(Match.objects.values_list("pk", flat=True)) == native_ids
    phases = {
        row.external_id: row.local_match.season.phase
        for row in SourceMatch.objects.select_related("local_match__season")
    }
    assert phases == {
        "A1": "autumn",
        "A2": "autumn",
        "S1": "spring",
        "S2": "spring",
    }
    assert TeamParticipation.objects.filter(phase="autumn").count() == len(["T1", "T2"])
    after = snapshot()
    again = run(RepairOptions(scope=scope, apply=True, split_outdoor=True))
    assert snapshot() == after
    assert again.counts["fixtures_moved"] == 0


@pytest.mark.parametrize("stored_phase", ["", "autumn"])
def test_blocked_poule_moves_nothing(stored_phase: str) -> None:
    """A blocked poule changes neither its fixtures nor their source routing.

    ``""`` is a poule published before periods were recorded; ``autumn`` one
    whose period publication already recorded.
    """
    scope = legacy_scope()
    Pool.objects.filter(external_id="10").update(phase=stored_phase)
    autumn = playing_season("autumn", 2026)
    local = Pool.objects.select_related("local_pool").get(external_id="10").local_pool
    assert local is not None
    SeasonPool.objects.create(season=autumn, name=local.name, sport=local.sport)
    report = run(RepairOptions(scope=scope, apply=True, split_outdoor=True))
    pool = Pool.objects.get(external_id="10")
    assert {
        "kind": "pool",
        "id": pool.pk,
        "reason": "native_pool_name_taken",
    } in report.blocked
    # Nothing about the blocked poule was saved, including its period.
    assert pool.phase == stored_phase
    autumn_fixtures = SourceMatch.objects.filter(external_id__in=["A1", "A2"])
    assert {row.local_match.season_id for row in autumn_fixtures} == {scope.pk}
    assert SeasonPool.objects.get(pk=local.pk).season_id == scope.pk
    # Source queries follow the native records, not the new bindings.
    routed = set(
        SourceMatch.objects.filter(native_match_filter(scope.pk)).values_list(
            "external_id", flat=True
        )
    )
    assert {"A1", "A2"} <= routed
    assert not SourceMatch.objects.filter(
        native_match_filter(autumn.pk), external_id__in=["A1", "A2"]
    ).exists()
    # A new fixture of the blocked poule stays out of autumn as well.
    Importer(scope, timezone.now()).apply(
        "club_results",
        "CT1",
        {"MatchResult": [fixture("A3", "2026-10-03T13:30:00+0200", 10)]},
    )
    result = publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert not Match.objects.filter(season=autumn).exists()
    if stored_phase:
        assert {
            "kind": "match",
            "source_id": SourceMatch.objects.get(external_id="A3").pk,
            "reason": "season_repair_required",
        } in result["blocked"]
    # The independent spring poule still moved.
    spring = SourceMatch.objects.get(external_id="S1").local_match
    assert spring is not None
    assert spring.season.phase == "spring"


def test_batches_resume_after_the_last_poule() -> None:
    """A bounded batch reports where the next one continues."""
    scope = legacy_scope()
    first = run(RepairOptions(scope=scope, split_outdoor=True, limit=1))
    assert first.next_after == Pool.objects.order_by("pk").first().pk
    second = run(
        RepairOptions(
            scope=scope, split_outdoor=True, limit=1, after=first.next_after or 0
        )
    )
    assert second.counts["pools_seen"] == 1
    assert [entry["pool"] for entry in first.pools + second.pools] == list(
        Pool.objects.order_by("pk").values_list("pk", flat=True)
    )


def test_command_requires_explicit_scope_and_defaults_to_preview(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The management command is read-only unless --apply is given."""
    scope = legacy_scope()
    before = snapshot()
    call_command(
        "repair_competition_context", "--scope", str(scope.pk), "--split-outdoor"
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert snapshot() == before
