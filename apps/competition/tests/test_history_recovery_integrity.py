"""Wrapper integrity, honest proof, retained tables and reviewed recovery controls."""

from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.history import HistoryClient
from apps.competition.application.ports import FetchResult
from apps.competition.models import HistoricalResource, Match, Pool, PoolEntry
from apps.competition.services.history import pool_coverage, seed
from apps.competition.services.history_checkpoint import checkpoint
from apps.competition.services.history_editions import prepare_edition
from apps.competition.services.history_integrity import (
    standing_projection,
    unique_matches,
    unique_standings,
)
from apps.competition.services.history_recovery import apply_recovery, plan_recovery
from apps.competition.services.history_sites import apply_site, import_site_rows
from apps.competition.services.history_worker import next_resource
from apps.competition.services.importer import Importer
from apps.competition.services.provider_scheduler import ProviderTurn, TurnOptions
from apps.competition.services.publishing import publish_catalogue
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_history import FakeClient
from apps.competition.tests.test_history_editions import row, standing
from apps.competition.tests.test_history_sites import page_row


def edition_pool(source_id: str = "7") -> HistoricalResource:
    """Create a checkpoint anchored to a closed synthetic edition."""
    return seed(prepare_edition(2024).indoor, "app", "edition_pool", source_id)


def pool_data(rows: list[dict], played: int = 1) -> dict:
    """Use a complete, unfiltered official response without private fields."""
    return {
        "MatchResult": rows,
        "PoolStanding": standing(
            played, "T1", "T2", sport=rows[0]["HomeTeam"]["SportId"]
        ),
        "ResultsFiltered": False,
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_normalizer_copies_rows_and_enriches_pool_in_either_order(
    reverse: bool,
) -> None:
    """A missing pool enriches without last-row selection or caller mutation."""
    complete = row("M1", "2024-09-14T15:00:00+0200")
    thin = deepcopy(complete)
    thin.pop("Pool")
    values = [complete, thin] if reverse else [thin, complete]
    before = deepcopy(values)
    accepted = unique_matches(values)
    assert len(accepted) == 1
    assert accepted[0]["Pool"] == complete["Pool"]
    accepted[0]["HomeTeam"]["TeamName"] = "changed locally"
    assert values == before


def test_duplicate_offsets_compare_the_same_kickoff_instant() -> None:
    """Provider offset formatting cannot create a false identity conflict."""
    first = row("M1", "2024-09-14T15:00:00+0200")
    second = {**first, "MatchDateTime": "2024-09-14T13:00:00Z"}
    assert len(unique_matches([first, second])) == 1


def test_matching_duplicate_rows_retain_later_allowlisted_context() -> None:
    """Wrapper collapse must retain rich source context without private fields."""
    first = row("M1", "2024-09-14T15:00:00+0200")
    second = deepcopy(first)
    second["HomeTeam"].update(
        Gender="Gemengd", Class=[{"ClassId": "public", "ClassName": "Example"}]
    )
    second["Pool"]["CompetitionKind"] = "COMPETITION"
    second["RoundNr"] = 2
    accepted = unique_matches([first, second])[0]
    assert accepted["HomeTeam"]["Gender"] == "Gemengd"
    assert accepted["Pool"]["CompetitionKind"] == "COMPETITION"
    assert accepted["RoundNr"] == second["RoundNr"]


@pytest.mark.parametrize("total", [None, True, -1, "1", 1.5])
def test_played_count_proof_rejects_unknown_or_coerced_values(total: object) -> None:
    """Invalid official counts cannot make historical coverage complete."""
    value = standing(1, "T1")["PoolStandingTeam"][0]
    value["TotalMatches"] = total
    with pytest.raises(ValueError, match="TotalMatches"):
        unique_standings([value])


def test_duplicate_standing_teams_reject_conflicting_values() -> None:
    """A repeated official team cannot arbitrarily select its last count."""
    value = standing(1, "T1")["PoolStandingTeam"][0]
    with pytest.raises(ValueError, match="Conflicting duplicate standing"):
        unique_standings([value, {**value, "TotalMatches": 2}])


def test_retained_standing_class_context_excludes_nested_private_fields() -> None:
    """Nested public class context keeps only validated provider IDs and names."""
    table = standing(1, "T1")
    table["PoolStandingTeam"][0]["Class"] = [
        {
            "ClassId": "public-class",
            "ClassName": "Public class",
            "BirthDate": "private-date",
            "Person": {"private": "value"},
        }
    ]
    projected = standing_projection(table)
    assert projected is not None
    assert projected["PoolStandingTeam"][0]["Class"] == [
        {"ClassId": "public-class", "ClassName": "Public class"}
    ]


@pytest.mark.django_db
def test_edition_duplicate_finals_do_not_prove_two_played_matches() -> None:
    """The actual wrapper certifies its unique persisted set only."""
    resource = edition_pool()
    fixture = row("M1", "2024-09-14T15:00:00+0200")
    checkpoint(resource, pool_data([fixture, deepcopy(fixture)], played=2))
    assert Match.objects.count() == 1
    assert resource.coverage == "partial"
    assert resource.evidence["observed_played"] == {"T1": 1, "T2": 1}
    assert resource.evidence["accepted_match_ids"] == ["M1"]


@pytest.mark.django_db
def test_conflicting_wrapper_rows_leave_archive_retirement_untouched() -> None:
    """Checkpoint conflicts roll back every source write and archive retirement."""
    resource = edition_pool()
    fixture = row("M1", "2024-09-14T15:00:00+0200")
    archived = {**fixture, "PublicMatchId": "archive:synthetic:1"}
    Importer(prepare_edition(2024).autumn, timezone.now(), discover=False).apply(
        "club_results", "", {"MatchResult": [archived]}
    )
    conflict = deepcopy(fixture)
    conflict["HomeResult"]["Score"] += 1
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        checkpoint(resource, pool_data([fixture, conflict]))
    assert list(Match.objects.values_list("external_id", flat=True)) == [
        archived["PublicMatchId"]
    ]
    resource.refresh_from_db()
    assert resource.state == "pending"


@pytest.mark.django_db
def test_archive_replacement_preserves_schedule_receipt_and_event_identity() -> None:
    """A provider replacement adopts its baseline without reviving schedule jobs."""
    seasons = prepare_edition(2024)
    resource = edition_pool()
    fixture = row("M1", "2024-09-14T15:00:00+0200")
    archived = {**fixture, "PublicMatchId": "archive:synthetic:1"}
    Importer(seasons.autumn, timezone.now(), discover=False).apply(
        "club_results", "", {"MatchResult": [archived]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    twin = Match.objects.get()
    assert twin.local_match_id is not None
    assert twin.published_schedule["fixture"]
    receipt = deepcopy(twin.published_schedule)
    event_id = uuid4()
    twin.schedule_notification_id = event_id
    twin.save(update_fields=("schedule_notification_id",))

    checkpoint(resource, pool_data([fixture]))
    successor = Match.objects.get()
    assert successor.external_id == "M1"
    assert successor.local_match_id == twin.local_match_id
    assert successor.local_created == twin.local_created
    assert successor.published_schedule == receipt
    assert successor.schedule_notification_id == event_id

    dispatch = RecordingScheduleChanges()
    publish_catalogue(schedule_changes=dispatch)
    successor.refresh_from_db()
    assert successor.schedule_notification_id == event_id
    assert dispatch.calls == []


@pytest.mark.django_db
def test_self_fixture_cannot_count_toward_wrapper_completeness() -> None:
    """Rejected self fixtures appear in exclusions rather than coverage totals."""
    resource = edition_pool()
    fixture = row("M1", "2024-09-14T15:00:00+0200")
    fixture["AwayTeam"] = deepcopy(fixture["HomeTeam"])
    checkpoint(resource, pool_data([fixture]))
    assert not Match.objects.exists()
    assert resource.coverage == "partial"
    assert resource.evidence["skipped"] == {"self_fixture": 1}


@pytest.mark.django_db
def test_outdoor_standings_only_are_retained_then_replayed() -> None:
    """An undated outdoor table cannot invent a half from its indoor anchor."""
    resource = edition_pool()
    table = standing(1, "T1", "T2")
    table["PoolStandingTeam"][0]["Person"] = {"private": "discard"}
    checkpoint(
        resource, {"MatchResult": [], "PoolStanding": table, "ResultsFiltered": False}
    )
    assert not Pool.objects.exists()
    assert resource.coverage == "partial"
    assert resource.reason == "standings_without_dated_results"
    assert "Person" not in resource.evidence["official_table"]["PoolStandingTeam"][0]
    assert HistoricalResource.objects.filter(
        kind="edition_team", source_id="T1"
    ).exists()
    checkpoint(
        resource,
        {
            "MatchResult": [row("M1", "2024-09-14T15:00:00+0200")],
            "PoolStanding": None,
            "ResultsFiltered": False,
        },
    )
    assert set(PoolEntry.objects.values_list("standing", flat=True)[0]) == {
        "TotalMatches"
    }


@pytest.mark.django_db
def test_indoor_standings_only_have_proven_binding_but_no_schedule_completeness() -> (
    None
):
    """Known indoor discipline routes the table without claiming dated coverage."""
    resource = edition_pool()
    checkpoint(
        resource,
        {
            "MatchResult": [],
            "PoolStanding": standing(1, "T1", "T2", sport="KORFBALL-ZA-WK"),
            "ResultsFiltered": False,
        },
    )
    assert Pool.objects.get().season_id == prepare_edition(2024).indoor.pk
    assert resource.coverage == "partial"
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_absent_table_preserves_official_values_but_explicit_empty_clears() -> None:
    """An omitted component is not a complete authoritative replacement."""
    resource = edition_pool()
    checkpoint(resource, pool_data([row("M1", "2024-09-14T15:00:00+0200")]))
    pool = Pool.objects.get()
    before = list(PoolEntry.objects.order_by("pk").values_list("standing", flat=True))
    importer = Importer(pool.season, timezone.now(), discover=False)
    importer.apply(
        "pool_results",
        "7",
        {"MatchResult": [], "PoolStanding": None, "ResultsFiltered": False},
    )
    assert (
        list(PoolEntry.objects.order_by("pk").values_list("standing", flat=True))
        == before
    )
    importer.apply(
        "pool_results",
        "7",
        {
            "MatchResult": [],
            "PoolStanding": {"PoolStandingTeam": []},
            "ResultsFiltered": False,
        },
    )
    assert all(
        value == {} for value in PoolEntry.objects.values_list("standing", flat=True)
    )


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["pending", "fetched", "blocked", "failed"])
def test_repeated_incomplete_summary_preserves_detail_attempt_lifecycle(
    state: str,
) -> None:
    """Bulk observations cannot reset detail backoff, ceilings or terminal state."""
    resource = edition_pool()
    fixture = row("M1", "2024-09-14T15:00:00+0200")
    fixture.update(Status="SUSPENDED", HomeResult={"Score": None})
    checkpoint(resource, pool_data([fixture]))
    detail = HistoricalResource.objects.get(kind="match", source_id="M1")
    assert detail.state == "pending"
    detail.state, detail.attempts, detail.reason = (
        state,
        7,
        "preserve_terminal_or_backoff",
    )
    detail.next_attempt_at = timezone.now() + timedelta(days=1)
    detail.fetched_at = timezone.now() if state != "pending" else None
    detail.evidence["detail_attempted"] = True
    detail.save()
    before = (
        detail.state,
        detail.attempts,
        detail.reason,
        detail.next_attempt_at,
        detail.fetched_at,
    )
    checkpoint(resource, pool_data([fixture]))
    detail.refresh_from_db()
    assert (
        detail.state,
        detail.attempts,
        detail.reason,
        detail.next_attempt_at,
        detail.fetched_at,
    ) == before
    assert detail.evidence["detail_attempted"] is True


@pytest.mark.django_db
def test_site_wrapper_copies_inputs_and_rejects_conflicts_before_routing() -> None:
    """Full-year and label cleanup cannot mutate caller-owned normalized rows."""
    resource = edition_pool()
    fixture = row("archive:synthetic:1", "2024-09-14T15:00:00+0200")
    fixture["Pool"]["FullYear"] = True
    before = deepcopy(fixture)
    import_site_rows(resource, [fixture, deepcopy(fixture)])
    assert fixture == before
    conflict = deepcopy(fixture)
    conflict["AwayResult"]["Score"] += 1
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        import_site_rows(resource, [fixture, conflict])
    assert Match.objects.count() == 1


@pytest.mark.django_db
def test_site_app_precedence_is_partial_all_rows_skipped() -> None:
    """Source precedence remains distinct from a genuinely empty site response."""
    resource = edition_pool()
    fixture = row("M1", "2024-11-09T14:00:00Z", sport="KORFBALL-ZA-WK")
    checkpoint(resource, pool_data([fixture]))
    site = HistoricalResource.objects.create(
        key="site_skipped",
        season=resource.season,
        provider="uitslagen",
        kind="match_page",
        source_id="0",
        start_date=resource.start_date,
        end_date=resource.end_date,
    )
    apply_site(site, {"rows": [page_row(1)]})
    assert site.coverage == "partial"
    assert site.reason == "all_rows_skipped"
    assert site.evidence["skipped"] == {"app_has_poule": 1}


def test_recovery_rejects_an_empty_generator_scope_before_reading() -> None:
    """A consumed iterable must not silently broaden a reviewed selection."""
    with pytest.raises(ValueError, match="requires an edition"):
        plan_recovery(resource_ids=iter(()))


@pytest.mark.django_db
def test_recovery_preview_writes_no_checkpoints_and_has_stable_cursor(
    tmp_path: Path,
) -> None:
    """Manifest creation only reads a bounded selected slice and writes local JSON."""
    first, second = edition_pool("7"), edition_pool("8")
    before = list(HistoricalResource.objects.order_by("pk").values())
    path = tmp_path / "review.json"
    call_command(
        "import_competition_history",
        "plan",
        edition=[2024],
        manifest=path,
        kinds=["edition_pool"],
        limit=1,
        max_requests=1,
        stdout=StringIO(),
    )
    assert list(HistoricalResource.objects.order_by("pk").values()) == before
    manifest = json.loads(path.read_text())
    assert [value["id"] for value in manifest["rows"]] == [first.pk]
    assert (
        plan_recovery(
            editions=[2024],
            kinds=["edition_pool"],
            cursor=manifest["next_cursor"],
            limit=1,
        )["rows"][0]["id"]
        == second.pk
    )


@pytest.mark.django_db
def test_stale_recovery_manifest_rejects_whole_selection() -> None:
    """One stale before-value prevents all selected retries from being rearmed."""
    first, second = edition_pool("7"), edition_pool("8")
    HistoricalResource.objects.filter(pk__in=[first.pk, second.pk]).update(
        state="blocked", reason="review"
    )
    manifest = plan_recovery(resource_ids=[first.pk, second.pk], max_requests=2)
    HistoricalResource.objects.filter(pk=second.pk).update(attempts=3)
    with pytest.raises(ValueError, match="Stale"):
        apply_recovery(manifest)
    assert set(HistoricalResource.objects.values_list("state", flat=True)) == {
        "blocked"
    }


@pytest.mark.django_db
def test_recovery_rearms_only_reviewed_ids_and_scoped_selection_cannot_escape() -> None:
    """A capped drain cannot pick unrelated globally ready history work."""
    selected, other = edition_pool("7"), edition_pool("8")
    HistoricalResource.objects.filter(pk=selected.pk).update(
        state="failed", etag="prior", attempts=8
    )
    result = apply_recovery(plan_recovery(resource_ids=[selected.pk], max_requests=1))
    selected.refresh_from_db()
    assert (selected.state, selected.attempts, selected.etag) == ("pending", 0, "")
    assert result["resource_ids"] == [selected.pk]
    ready = next_resource(resource_ids=frozenset([selected.pk]))
    assert ready is not None
    assert ready.pk == selected.pk
    HistoricalResource.objects.filter(pk=selected.pk).update(state="fetched")
    assert next_resource(resource_ids=frozenset([selected.pk])) is None
    ready = next_resource()
    assert ready is not None
    assert ready.pk == other.pk


@pytest.mark.django_db
def test_scoped_provider_turn_fetches_no_unselected_ready_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real shared turn, rather than a capped global drain, enforces selection."""
    selected, other = edition_pool("7"), edition_pool("8")
    client = FakeClient([FetchResult(200, {"MatchResult": [], "PoolStanding": None})])
    monkeypatch.setattr("apps.competition.services.traffic.time.sleep", lambda _: None)
    result = ProviderTurn(
        None,
        lambda: (Mock(), client),
        TurnOptions(
            schedule_changes=None,
            publish=False,
            history_budget=1,
            history_resource_ids=frozenset([selected.pk]),
        ),
    ).run()
    selected.refresh_from_db()
    other.refresh_from_db()
    history = result["history"]
    assert isinstance(history, dict)
    assert history["http_requests"] == 1
    assert selected.state == "fetched"
    assert other.state == "pending"


@pytest.mark.django_db
def test_recovery_drain_yields_when_live_work_becomes_due(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New urgent work ends a selected drain before its remaining requests."""
    first, second = edition_pool("7"), edition_pool("8")
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps(plan_recovery(resource_ids=[first.pk, second.pk], max_requests=2))
    )
    client = FakeClient([FetchResult(200, {"MatchResult": [], "PoolStanding": None})])
    module = "apps.competition.management.commands.import_competition_history"
    monkeypatch.setattr(
        module + ".history_client",
        lambda _: Mock(app=Mock(), fetch=client.fetch, close=client.close),
    )
    monkeypatch.setattr(module + ".current_work_due", lambda: bool(client.calls))
    monkeypatch.setattr("apps.competition.services.traffic.time.sleep", lambda _: None)
    call_command(
        "import_competition_history",
        "apply-plan",
        manifest=path,
        drain=True,
        stdout=StringIO(),
    )
    first.refresh_from_db()
    second.refresh_from_db()
    assert client.calls == ["edition_pool"]
    assert first.state == "fetched"
    assert second.state == "pending"


@pytest.mark.django_db
def test_detail_pool_move_refreshes_both_edition_checkpoint_proofs() -> None:
    """An enrichment correction invalidates old counts and recomputes its new pool."""
    first, second = edition_pool("7"), edition_pool("8")
    fixture = row("M1", "2024-09-14T15:00:00+0200")
    checkpoint(first, pool_data([fixture]))
    next_fixture = row("M2", "2024-09-21T15:00:00+0200", pool=8)
    checkpoint(second, pool_data([next_fixture]))
    assert first.coverage == second.coverage == "complete"
    detail = HistoricalResource.objects.get(kind="match", source_id="M1")
    corrected = deepcopy(fixture)
    corrected["Pool"]["PoolId"] = 8
    checkpoint(detail, corrected)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.coverage == "partial"
    # The second checkpoint did not observe the added fixture in its own source
    # response, so it cannot continue asserting a complete schedule.
    assert second.coverage == "partial"


@pytest.mark.django_db
def test_legacy_generated_standings_cannot_certify_their_own_fixture_counts() -> None:
    """Independent official counts are required even when saved finals agree."""
    seasons = prepare_edition(2024)
    resource = seed(seasons.autumn, "app", "pool", "7")
    checkpoint(resource, pool_data([row("M1", "2024-09-14T15:00:00+0200")]))
    for entry in PoolEntry.objects.all():
        entry.standing = {**entry.standing, "Computed": True}
        entry.save(update_fields=("standing",))
    assert pool_coverage(resource, {"ResultsFiltered": False})[0] == "partial"


@pytest.mark.django_db
def test_generic_spring_pool_request_selects_previous_july_edition() -> None:
    """The app SeasonId is a provider edition rather than spring's calendar year."""
    resource = seed(prepare_edition(2024).spring, "app", "pool", "7")
    app = Mock()
    app.store = None
    response = Mock(status_code=200, headers={})
    response.json.return_value = {"MatchResult": []}
    app._get.return_value = response
    client = HistoryClient(app)
    try:
        client.fetch_app(resource, Mock())
        assert app._get.call_args.kwargs["params"]["SeasonId"] == "2024"
    finally:
        client.close()
