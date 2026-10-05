"""Metadata context fences and conservative source/native fixture reconciliation."""

from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.db.models import QuerySet
from django.utils import timezone
import pytest

from apps.competition.application.ports import FetchResult
from apps.competition.domain.source_context import context_value
from apps.competition.models import Match, MatchMembership, Pool, SyncResource
from apps.competition.queries.match_info import match_info
from apps.competition.services.importer import Importer
from apps.competition.services.linkage_review import (
    LinkageSelection,
    apply_manifest,
    preview_linkage,
)
from apps.competition.services.match_details import (
    DetailSelection,
    capture_metadata_context,
    component_state,
    detail_candidates,
    import_facility,
    preview_details,
)
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.seasons import configure_seasons
from apps.competition.services.sync import sync_details
from apps.competition.services.traffic import TrafficGate
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload, team_payload
from apps.competition.tests.test_match_details import details
from apps.game_tracker.models import MatchData
from apps.player.models import Player
from apps.schedule.models import (
    Match as NativeMatch,
    Season,
)


@pytest.mark.django_db
def test_reschedule_stales_venue_and_preserves_failed_backoff(season: Season) -> None:
    """An old venue stays retained for review but cannot be shown as verified."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    Importer(season, now).apply("match_facility", "M1", details("match_facility"))
    Importer(season, now).apply("match_rules", "M1", details("match_rules"))
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    resource = SyncResource.objects.get(kind="match_facility", source_id="M1")
    retry_at = now + timedelta(days=2)
    resource.failures, resource.next_sync_at = 2, retry_at
    resource.save(update_fields=("failures", "next_sync_at"))
    row["MatchDateTime"] = "2026-09-06T13:30:00+0200"
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_results", "C", {"MatchResult": [row]}
    )
    source = Match.objects.get()
    assert component_state(source, "match_facility") == "stale"
    assert source.facility_observed_at is None
    assert source.facility_details == details("match_facility")
    assert component_state(source, "match_rules") == "available"
    assert match_info(source.local_match)["venue"] is None
    resource.refresh_from_db()
    assert (resource.failures, resource.next_sync_at) == (2, retry_at)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["match_facility", "match_rules", "match_timing"])
def test_response_context_is_captured_before_io(season: Season, kind: str) -> None:
    """A response from the former home team cannot certify a corrected fixture."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    captured = capture_metadata_context(season, "M1", kind)
    row["HomeTeam"] = team_payload("T3")
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_results", "C", {"MatchResult": [row]}
    )
    Importer(
        season, now + timedelta(seconds=2), expected_metadata_context=captured
    ).apply(kind, "M1", details(kind))
    source = Match.objects.get()
    assert kind not in source.metadata_observations
    assert component_state(source, kind) == "unobserved"


@pytest.mark.django_db
def test_empty_facility_has_a_later_refresh_not_permanent_completion(
    season: Season,
) -> None:
    """An observed null venue remains empty and waits for the empty-result interval."""
    now = timezone.now()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [match_payload()]})
    import_facility(season, "M1", {"FacilityName": None, "Address": ""}, now)
    source = Match.objects.get()
    assert component_state(source, "match_facility") == "empty"
    assert not detail_candidates(season, "match_facility", now).exists()
    assert detail_candidates(
        season, "match_facility", now + timedelta(days=31)
    ).exists()
    assert (
        preview_details(season, selection=DetailSelection(kinds=("match_facility",)))[
            "states_by_kind"
        ]["match_facility"]["empty"]
        == 1
    )


@pytest.mark.django_db
def test_app_only_scoped_preview_does_not_write_or_open_client(season: Season) -> None:
    """Keep archive identities outside the app endpoint request budget."""
    now = timezone.now()
    for identity in ("M1", "M2", "archive:synthetic"):
        row = {**match_payload(), "PublicMatchId": identity}
        Importer(season, now, discover=False).apply(
            "club_results", "C", {"MatchResult": [row]}
        )
    original = list(Match.objects.values())
    output = StringIO()
    with patch(
        "apps.competition.management.commands.sync_competition.competition_client",
        side_effect=AssertionError("dry-run opened a provider"),
    ):
        call_command(
            "update_competition_match_details",
            season=season.name,
            dry_run=True,
            component=["match_facility"],
            source_id=["M1", "archive:synthetic"],
            limit=10,
            stdout=output,
        )
    preview = json.loads(output.getvalue())
    assert preview["remaining_detail_requests"] == 1
    assert preview["unsupported_matches"] == 1
    assert preview["selected_source_ids"] == ["M1"]
    assert not SyncResource.objects.exists()
    assert list(Match.objects.values()) == original
    with pytest.raises(ValueError, match="Unsupported"):
        import_facility(season, "archive:synthetic", details("match_facility"), now)


@pytest.mark.django_db
def test_http_response_during_reschedule_is_deferred_and_unobserved(
    season: Season,
) -> None:
    """The real checkpoint path keeps a stale in-flight response retryable."""
    now = timezone.now()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [match_payload()]})
    client = Mock()

    def fetch(resource: SyncResource, gate: TrafficGate) -> FetchResult:
        gate.before_request()
        source = Match.objects.get()
        Match.objects.filter(pk=source.pk).update(
            starts_at=source.starts_at + timedelta(days=1)
        )
        return FetchResult(200, details("match_facility"))

    client.fetch.side_effect = fetch
    with patch("apps.competition.services.sync.backfill_spacing", return_value=0):
        summary = sync_details(
            season,
            lambda: client,
            budget=1,
            kinds=("match_facility",),
            source_ids=("M1",),
        )
    assert summary["http_requests"] == 1
    source = Match.objects.get()
    resource = SyncResource.objects.get(kind="match_facility", source_id="M1")
    assert source.facility_observed_at is None
    assert source.facility_details == {}
    assert now < resource.next_sync_at < now + timedelta(minutes=10)


@pytest.mark.django_db
def test_retained_context_does_not_dirty_identical_result(season: Season) -> None:
    """Freshness-only observations cannot reopen publication or ratings work."""
    now = timezone.now()
    row = {**match_payload(), "RoundNr": 3, "ExternalMatchId": 42}
    Importer(season, now, discover=False).apply(
        "club_results", "C", {"MatchResult": [row]}
    )
    source = Match.objects.get()
    updated_at, context = source.updated_at, source.source_context
    Importer(season, now + timedelta(days=1), discover=False).apply(
        "club_results", "C", {"MatchResult": [row]}
    )
    source.refresh_from_db()
    assert source.updated_at == updated_at
    assert context_value(source.source_context, "RoundNr") == context_value(
        context, "RoundNr"
    )
    assert context_value(source.source_context, "ExternalMatchId") == context_value(
        context, "ExternalMatchId"
    )
    assert source.result_observed_at == now + timedelta(days=1)


@pytest.mark.django_db
@pytest.mark.parametrize("protection", ["legacy", "manual", "native_edit", "tracking"])
def test_fixture_changes_require_untouched_importer_baseline(
    season: Season, protection: str
) -> None:
    """Source kickoff corrections cannot overwrite local or unproven native history."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = Match.objects.get()
    local = source.local_match
    assert (
        source.published_schedule["fixture"]["starts_at"]
        == local.start_time.isoformat()
    )
    if protection == "legacy":
        source.published_schedule.pop("fixture")
        source.save(update_fields=("published_schedule",))
    elif protection == "manual":
        source.local_created = False
        source.save(update_fields=("local_created",))
    elif protection == "native_edit":
        NativeMatch.objects.filter(pk=local.pk).update(
            start_time=local.start_time + timedelta(hours=1)
        )
        local.refresh_from_db()
    else:
        MatchData.objects.filter(match_link=local).update(live_revision=1)
    original = local.start_time
    row["MatchDateTime"] = "2026-09-06T13:30:00+0200"
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_results", "C", {"MatchResult": [row]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    local.refresh_from_db()
    assert local.start_time == original


@pytest.mark.django_db
@pytest.mark.parametrize("with_selection", [False, True])
def test_participant_correction_preserves_dependent_selections(
    season: Season, with_selection: bool
) -> None:
    """Protect sporting dependencies when source participants change."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = Match.objects.get()
    original_home = source.local_match.home_team_id
    if with_selection:
        MatchMembership.objects.create(
            match=source,
            team=source.home_team,
            player=Player.objects.create(name="Synthetic"),
            observed_at=now,
            role="selected",
        )
    row["HomeTeam"] = team_payload("T3")
    row["HomeResult"]["Score"] = 7
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_results", "C", {"MatchResult": [row]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source.refresh_from_db()
    source.local_match.refresh_from_db()
    assert (source.local_match.home_team_id == original_home) is with_selection
    tracker = MatchData.objects.get(match_link=source.local_match)
    assert tracker.home_score == (0 if with_selection else row["HomeResult"]["Score"])
    if with_selection:
        assert source.lineup.count() == 1


@pytest.mark.django_db
def test_linkage_manifest_rechecks_native_before_values(season: Season) -> None:
    """Block stale previews and retain ambiguous duplicates for review."""
    now = timezone.now()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [match_payload()]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = Match.objects.get()
    Match.objects.filter(pk=source.pk).update(
        starts_at=source.starts_at + timedelta(days=1)
    )
    selection = LinkageSelection(source_ids=("M1",))
    manifest = preview_linkage(selection)
    assert manifest["counts"] == {"safe_update": 1}
    assert preview_linkage(selection) == manifest
    native = source.local_match
    NativeMatch.objects.filter(pk=native.pk).update(
        start_time=native.start_time + timedelta(hours=1)
    )
    assert apply_manifest(manifest) == {"stale_manifest": 1}
    native.refresh_from_db()
    NativeMatch.objects.create(
        season=native.season,
        home_team=native.home_team,
        away_team=native.away_team,
        pool=native.pool,
        start_time=native.start_time,
    )
    assert preview_linkage(selection)["counts"] == {"duplicate_native_review": 1}


@pytest.mark.django_db
def test_linkage_waits_for_an_unresolved_competition_period(season: Season) -> None:
    """A split outdoor scope never falls back to its annual season for linking."""
    configure_seasons(season, 2026, split_outdoor=True)
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = Match.objects.get()
    native = source.local_match
    assert native is not None
    # An annual native fixture and a source row whose period is unknown again.
    NativeMatch.objects.filter(pk=native.pk).update(season=season, pool=None)
    Match.objects.filter(pk=source.pk).update(local_match=None, local_created=False)
    Pool.objects.update(phase="", phase_evidence={}, local_pool=None)
    manifest = preview_linkage(LinkageSelection(source_ids=("M1",)))
    assert manifest["counts"] == {"period_review": 1}
    apply_manifest(manifest)
    assert Match.objects.get().local_match_id is None


@pytest.mark.django_db
def test_superseded_selection_links_become_reachable_without_copying_players(
    season: Season,
) -> None:
    """Relink selections to the exact final twin and retain both source records."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    row = deepcopy(row)
    row.update(PublicMatchId="M2", Status="SUSPENDED")
    Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    source = Match.objects.get(external_id="M2")
    source.lineup_observed_at = now
    source.save(update_fields=("lineup_observed_at",))
    player = Player.objects.create(name="Synthetic")
    membership = MatchMembership.objects.create(
        match=source,
        team=source.home_team,
        player=player,
        observed_at=now,
        role="selected",
    )
    manifest = preview_linkage(LinkageSelection(source_ids=("M2",)))
    assert manifest["counts"] == {"safe_selection_relink": 1}
    assert apply_manifest(manifest) == {"applied": 1}
    membership.refresh_from_db()
    assert membership.match.external_id == "M1"
    assert membership.match.local_match_id is not None
    assert Player.objects.count() == 1
    assert Match.objects.count() == len(["M1", "M2"])
    assert NativeMatch.objects.count() == 1
    assert preview_linkage(LinkageSelection(source_ids=("M2",)))["counts"] == {
        "superseded": 1
    }


@pytest.mark.django_db
def test_stale_result_cannot_revert_nested_catalogue_before_guard(
    season: Season,
) -> None:
    """Reject delayed results before their team/pool observations can mutate links."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = Match.objects.select_related("home_team", "away_team", "pool").get()
    original = (
        source.home_team.sport,
        source.home_team.local_team_data_id,
        source.pool.class_name,
    )
    stale = deepcopy(row)
    for side in ("HomeTeam", "AwayTeam"):
        stale[side]["SportId"] = "KORFBALL-ZA-WK"
    stale["Pool"]["ClassName"] = "Old discarded class"
    Importer(season, now - timedelta(seconds=1)).apply(
        "club_results", "C", {"MatchResult": [stale]}
    )
    source.refresh_from_db()
    source.home_team.refresh_from_db()
    source.pool.refresh_from_db()
    assert (
        source.home_team.sport,
        source.home_team.local_team_data_id,
        source.pool.class_name,
    ) == original


@pytest.mark.django_db
def test_linkage_manifest_uses_locked_native_reread(season: Season) -> None:
    """A native edit between preload and lock must invalidate the preview."""
    now = timezone.now()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [match_payload()]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = Match.objects.get()
    native = source.local_match
    Match.objects.filter(pk=source.pk).update(
        starts_at=source.starts_at + timedelta(days=1)
    )
    manifest = preview_linkage(LinkageSelection(source_ids=("M1",)))
    lock = NativeMatch.objects.select_for_update
    edited_time = native.start_time + timedelta(hours=1)

    def reread(*, no_key: bool = False) -> QuerySet[NativeMatch]:
        NativeMatch.objects.filter(pk=native.pk).update(start_time=edited_time)
        return lock(no_key=no_key)

    with patch.object(NativeMatch.objects, "select_for_update", side_effect=reread):
        assert apply_manifest(manifest) == {"stale_manifest": 1}
    native.refresh_from_db()
    assert native.start_time == edited_time


@pytest.mark.django_db
def test_scoped_detail_drain_does_not_consume_or_report_unrelated_exhaustion(
    season: Season,
) -> None:
    """Keep a capped metadata request and its diagnostics inside the selected IDs."""
    now = timezone.now()
    for identity in ("M1", "M2"):
        row = {**match_payload(), "PublicMatchId": identity}
        Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    SyncResource.objects.filter(kind="match_facility", source_id="M2").update(
        failures=6
    )
    client = Mock()

    def fetch(resource: SyncResource, gate: TrafficGate) -> FetchResult:
        gate.before_request()
        assert (resource.kind, resource.source_id) == ("match_facility", "M1")
        return FetchResult(200, details("match_facility"))

    client.fetch.side_effect = fetch
    with patch("apps.competition.services.sync.backfill_spacing", return_value=0):
        result = sync_details(
            season,
            lambda: client,
            budget=1,
            kinds=("match_facility",),
            source_ids=("M1",),
        )
    assert result["exhausted"] == 0
    assert result["http_requests"] == 1
    unrelated = SyncResource.objects.get(kind="match_facility", source_id="M2")
    assert unrelated.fetched_at is None
    assert unrelated.failures == len(range(6))


def _selection_relink_case(season: Season) -> tuple[Match, Match, MatchMembership]:
    """Prepare one source selection on a suspended alias beside an exact final."""
    now = timezone.now()
    row = match_payload()
    Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    row = deepcopy(row)
    row.update(PublicMatchId="M2", Status="SUSPENDED")
    Importer(season, now).apply("club_results", "C", {"MatchResult": [row]})
    source = Match.objects.get(external_id="M2")
    source.lineup_observed_at = now
    source.save(update_fields=("lineup_observed_at",))
    membership = MatchMembership.objects.create(
        match=source,
        team=source.home_team,
        player=Player.objects.create(name="Synthetic"),
        observed_at=now,
        role="selected",
    )
    return source, Match.objects.get(external_id="M1"), membership


@pytest.mark.django_db
@pytest.mark.parametrize("edit", ["participants", "season"])
def test_selection_relink_requires_compatible_native_target(
    season: Season, edit: str
) -> None:
    """A matching source twin cannot override a different native sporting identity."""
    source, target, membership = _selection_relink_case(season)
    native = target.local_match
    if edit == "participants":
        other_team = native.away_team.__class__.objects.create(
            club=native.away_team.club, name="Independent synthetic team"
        )
        NativeMatch.objects.filter(pk=native.pk).update(away_team=other_team)
    else:
        other_season = Season.objects.create(
            name="Independent synthetic season",
            start_date=season.start_date,
            end_date=season.end_date,
        )
        NativeMatch.objects.filter(pk=native.pk).update(season=other_season)
    manifest = preview_linkage(LinkageSelection(source_ids=("M2",)))
    assert manifest["counts"] == {"protected_selection_review": 1}
    assert apply_manifest(manifest) == {"review_only": 1}
    membership.refresh_from_db()
    assert membership.match.pk == source.pk


@pytest.mark.django_db
def test_selection_relink_rechecks_locked_native_target(season: Season) -> None:
    """A target edit between preload and lock must leave selection links unchanged."""
    source, target, membership = _selection_relink_case(season)
    native = target.local_match
    manifest = preview_linkage(LinkageSelection(source_ids=("M2",)))
    assert manifest["counts"] == {"safe_selection_relink": 1}
    lock = NativeMatch.objects.select_for_update
    edited_time = native.start_time + timedelta(hours=1)

    def reread(*, no_key: bool = False) -> QuerySet[NativeMatch]:
        NativeMatch.objects.filter(pk=native.pk).update(start_time=edited_time)
        return lock(no_key=no_key)

    with patch.object(NativeMatch.objects, "select_for_update", side_effect=reread):
        assert apply_manifest(manifest) == {"stale_manifest": 1}
    membership.refresh_from_db()
    native.refresh_from_db()
    assert membership.match.pk == source.pk
    assert native.start_time == edited_time
