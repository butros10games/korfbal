"""Test historical importing with fabricated identities and no credentials."""

from datetime import date, timedelta
from io import StringIO
import json
from unittest.mock import Mock

from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.history import LOOKBACK_WEEKS, HistoryClient
from apps.competition.application.ports import FetchResult, TransportError
from apps.competition.models import (
    Club,
    HistoricalDiscovery,
    HistoricalResource,
    Match,
    Pool,
    SyncLease,
    SyncResource,
    TrafficState,
)
from apps.competition.services.history import (
    HistoryUnavailableError,
    seed,
    split_window,
)
from apps.competition.services.history_archive import import_archive
from apps.competition.services.history_checkpoint import checkpoint
from apps.competition.services.history_dataservice import reconcile_pool_coverage
from apps.competition.services.history_worker import run_history
from apps.competition.services.publishing import publish_catalogue
from apps.competition.tests.test_importer import match_payload, team_payload
from apps.game_tracker.models import MatchData
from apps.game_tracker.services.tracker_state import get_tracker_state
from apps.schedule.models import Season


@pytest.fixture
def old_season() -> Season:
    """Create a closed season independent of the current calendar year."""
    return Season.objects.create(
        name="history-2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )


def old_match() -> dict:
    """Fabricate a completed May match with its observed poule ID."""
    row = match_payload()
    row["MatchDateTime"] = "2025-05-10T13:30:00+0200"
    return row


def old_pool() -> dict:
    """One played fixture, with standings that independently agree with its result."""
    return {
        "ResultsFiltered": False,
        "MatchResult": [old_match()],
        "PoolStanding": {
            "PoolStandingTeam": [
                {**team_payload(k), "TotalMatches": 1} for k in ["T1", "T2"]
            ]
        },
    }


class FakeClient:
    """Count wire requests while replaying synthetic provider responses."""

    def __init__(self, replies: list) -> None:
        """Retain synthetic transport responses."""
        self.replies = iter(replies)
        self.calls = []

    def fetch(self, resource: HistoricalResource, gate: object) -> FetchResult:
        """Replay one counted provider response."""
        gate.before_request()
        self.calls.append(resource.kind)
        response = next(self.replies)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        """No real connections are opened."""


@pytest.fixture(autouse=True)
def no_spacing_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise durable reservations without sleeping in regression tests."""
    monkeypatch.setattr("apps.competition.services.traffic.time.sleep", lambda _: None)


@pytest.mark.django_db
def test_seed_deduplicates_and_retains_provenance(old_season: Season) -> None:
    """A second parent or overlapping window never resets a completed resource."""
    a = seed(
        old_season,
        "app",
        "match",
        "M1",
        reference="https://example.org/archive?token=private",
    )
    a.state = "fetched"
    a.save()
    b = seed(
        old_season,
        "app",
        "match",
        "M1",
        reference="another-source",
        start=date(2025, 5, 1),
        end=date(2025, 5, 31),
    )
    assert a.pk == b.pk
    assert b.state == "fetched"
    assert HistoricalDiscovery.objects.count() == len(["first", "second"])
    assert a.discoveries.filter(reference="https://example.org/archive").exists()


@pytest.mark.django_db
def test_match_pool_chain_reuses_native_source_models(old_season: Season) -> None:
    """Historical data creates no present-day discovery work and resumes in two runs."""
    seed(old_season, "app", "match", "M1")
    first = FakeClient([FetchResult(200, old_match())])
    assert run_history(lambda: first, budget=1, publish=False)["http_requests"] == 1
    assert not SyncResource.objects.exists()
    second = FakeClient([FetchResult(200, old_pool())])
    run_history(lambda: second, budget=1, publish=False)
    pool = HistoricalResource.objects.get(kind="pool")
    assert pool.coverage == "complete"
    assert Match.objects.count() == 1
    assert Pool.objects.count() == 1
    assert not SyncResource.objects.exists()
    assert run_history(lambda: FakeClient([]), publish=False)["http_requests"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change", ["filtered", "missing", "wrong_count", "unknown_count"]
)
def test_pool_coverage_never_assumes_complete(old_season: Season, change: str) -> None:
    """Filtered, missing or contradictory fixture evidence stays partial."""
    resource = seed(old_season, "app", "pool", "10")
    data = old_pool()
    if change == "filtered":
        data["ResultsFiltered"] = True
    if change == "missing":
        data["MatchResult"][0]["AwayResult"]["Score"] = None
    if change == "wrong_count":
        data["PoolStanding"]["PoolStandingTeam"][0]["TotalMatches"] = 2
    if change == "unknown_count":
        data["PoolStanding"]["PoolStandingTeam"][0].pop("TotalMatches")
    checkpoint(resource, data)
    resource.refresh_from_db()
    assert resource.coverage == "partial"


@pytest.mark.django_db
def test_empty_pool_does_not_publish_undated_standings(old_season: Season) -> None:
    """An empty results list cannot prove that an upstream ID belongs to this season."""
    resource = seed(old_season, "app", "pool", "10")
    data = old_pool()
    data["MatchResult"] = []
    checkpoint(resource, data)
    assert resource.coverage == "empty"
    assert not Pool.objects.exists()


@pytest.mark.django_db
def test_wrong_season_rolls_back_and_blocks(old_season: Season) -> None:
    """Current-season data cannot silently populate an old season."""
    resource = seed(old_season, "app", "match", "M1")
    client = FakeClient([FetchResult(200, match_payload())])
    run_history(lambda: client, publish=False)
    resource.refresh_from_db()
    assert resource.reason == "season_mismatch"
    assert resource.state == "blocked"
    assert not Match.objects.exists()
    assert not Club.objects.exists()


@pytest.mark.django_db
def test_failures_backoff_without_global_rate_fallback(old_season: Season) -> None:
    """A temporary failure does not consume every retry in one batch."""
    resource = seed(old_season, "app", "match", "M1")
    client = FakeClient([TransportError("unavailable")])
    run_history(lambda: client, publish=False)
    resource.refresh_from_db()
    assert resource.attempts == 1
    assert resource.state == "pending"
    assert not TrafficState.objects.get().rate_limited
    assert len(client.calls) == 1


@pytest.mark.django_db
def test_429_stops_batch_and_persists_conservative_policy(old_season: Season) -> None:
    """Rate limiting pauses all provider work and preserves the pending resource."""
    resource = seed(old_season, "app", "match", "M1")
    seed(old_season, "app", "match", "M2")
    client = FakeClient([FetchResult(429, retry_after=300)])
    result = run_history(lambda: client, publish=False)
    assert result["http_requests"] == 1
    assert TrafficState.objects.get().rate_limited
    assert SyncLease.objects.get().expires_at > timezone.now() + timedelta(seconds=290)
    resource.refresh_from_db()
    assert resource.state == "pending"
    assert resource.attempts == 0


@pytest.mark.django_db
def test_live_work_has_priority_and_credentials_are_lazy(old_season: Season) -> None:
    """Do not load credentials or send historical traffic while current work is due."""
    today = timezone.localdate()
    live = Season.objects.create(
        name="live",
        start_date=today - timedelta(days=1),
        end_date=today + timedelta(days=1),
    )
    SyncResource.objects.create(season=live, kind="clubs", next_sync_at=timezone.now())
    factory = Mock()
    assert run_history(factory)["reason"] == "current_work_due"
    factory.assert_not_called()


@pytest.mark.django_db
def test_split_windows_resume_without_overlap(old_season: Season) -> None:
    """Split boundaries are inclusive, gap-free and idempotent."""
    resource = seed(
        old_season,
        "dataservice",
        "window",
        "C1",
        start=date(2025, 5, 1),
        end=date(2025, 5, 31),
    )
    split_window(resource)
    resource.save()
    split_window(resource)
    children = list(
        HistoricalResource.objects.exclude(pk=resource.pk).order_by("start_date")
    )
    assert len(children) == len(["first", "second"])
    assert children[0].start_date == resource.start_date
    assert children[0].end_date + timedelta(days=1) == children[1].start_date
    assert children[1].end_date == resource.end_date


@pytest.mark.django_db
def test_single_day_truncation_is_blocked_partial(old_season: Season) -> None:
    """A row limit cannot silently discard excess same-day matches."""
    resource = seed(
        old_season,
        "dataservice",
        "window",
        "C1",
        start=date(2025, 5, 1),
        end=date(2025, 5, 1),
    )
    row = {**dataservice_row(), "wedstrijddatum": "2025-05-01T13:30:00+0200"}
    checkpoint(resource, {"rows": [row] * 500, "wire_start": resource.start_date})
    assert resource.state == "blocked"
    assert resource.coverage == "partial"


@pytest.mark.django_db
def test_dataservice_limit_sends_no_request(old_season: Season) -> None:
    """Do not repeatedly query dates beyond the documented lookback limit."""
    resource = seed(
        old_season,
        "dataservice",
        "window",
        "C1",
        start=date(2025, 1, 1),
        end=date(2025, 1, 2),
    )
    client = HistoryClient(dataservice_id="synthetic")
    gate = Mock()
    with pytest.raises(HistoryUnavailableError, match="52_week"):
        client.fetch(resource, gate)
    gate.before_request.assert_not_called()


@pytest.mark.django_db
def test_app_adapter_only_uses_verified_parameters(old_season: Season) -> None:
    """App authentication stays on its original host and preserves provider version."""
    resource = seed(old_season, "app", "match", "M1")
    app = Mock()
    app.store.needs_refresh.return_value = False
    response = Mock(status_code=200, headers={})
    response.json.return_value = old_match()
    app._get.return_value = response
    client = HistoryClient(app)
    client.fetch(resource, Mock())
    args, kwargs = app._get.call_args
    assert args[0].endswith("/app/match/MatchResultDetails")
    assert kwargs["params"] == {"PublicMatchId": "M1", "v": "8"}


@pytest.mark.django_db
def test_archive_is_idempotent_attributed_and_does_not_invent_scores(
    old_season: Season,
) -> None:
    """Archive rows preserve their own namespace and unknown scores remain unknown."""
    row = old_match()
    row["HomeResult"]["Score"] = None
    for side in ["HomeTeam", "AwayTeam"]:
        club = row[side]["Club"]
        Club.objects.create(external_id=club["ClubId"], name=club["ClubName"])
    doc = {
        "namespace": "clubbook",
        "source": "https://example.org/2025.pdf",
        "matches": [row],
    }
    assert import_archive(old_season, doc)["imported"] == 1
    assert import_archive(old_season, doc)["imported"] == 0
    match = Match.objects.get()
    assert match.external_id == "archive:clubbook:M1"
    assert match.home_score is None
    assert HistoricalDiscovery.objects.get().reference == doc["source"]
    assert not SyncResource.objects.exists()


@pytest.mark.django_db
def test_cli_seed_status_and_explicit_retry(old_season: Season) -> None:
    """Resume one checkpoint without resetting the whole crawl."""
    output = StringIO()
    call_command(
        "import_competition_history",
        "seed",
        season=old_season.name,
        source_id="M1",
        stdout=output,
    )
    resource = HistoricalResource.objects.get()
    resource.state = "blocked"
    resource.save()
    call_command(
        "import_competition_history", "retry", resource=resource.pk, stdout=StringIO()
    )
    resource.refresh_from_db()
    assert resource.state == "pending"
    output = StringIO()
    call_command("import_competition_history", "status", stdout=output)
    assert json.loads(output.getvalue())[0]["resources"] == 1


@pytest.mark.django_db
def test_historical_club_name_does_not_replace_current_catalogue(
    old_season: Season,
) -> None:
    """An old label cannot overwrite the present-day club identity."""
    Club.objects.create(external_id="CT1", name="Current name")
    checkpoint(seed(old_season, "app", "match", "M1"), old_match())
    assert Club.objects.get(external_id="CT1").name == "Current name"


@pytest.mark.django_db
def test_exact_match_interval_rejects_other_month(old_season: Season) -> None:
    """A response inside the season but outside the requested month stays unimported."""
    resource = seed(
        old_season, "app", "match", "M1", start=date(2025, 4, 1), end=date(2025, 4, 30)
    )
    with pytest.raises(HistoryUnavailableError, match="interval_mismatch"):
        checkpoint(resource, old_match())
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_factory_failure_releases_provider_lease() -> None:
    """Invalid credential files cannot leave an owned lease behind."""

    def fail() -> HistoryClient:
        raise ValueError("synthetic invalid configuration")

    with pytest.raises(ValueError, match="synthetic"):
        run_history(fail)
    assert SyncLease.objects.get().owner is None


@pytest.mark.django_db
def test_private_dataservice_key_is_not_sent_to_app(old_season: Season) -> None:
    """Separate provider sessions avoid leaking account tokens across API products."""
    resource = seed(old_season, "dataservice", "standing", "10")
    app = Mock()
    client = HistoryClient(app, dataservice_id="synthetic")
    client.session.get = Mock(return_value=Mock(status_code=200, headers={}, json=list))
    gate = Mock()
    client.fetch(resource, gate)
    app._get.assert_not_called()
    assert "Authorization" not in client.session.headers
    assert client.session.get.call_args.kwargs["params"] == {
        "client_id": "synthetic",
        "poulecode": "10",
    }
    gate.before_request.assert_called_once()


@pytest.mark.django_db
def test_dataservice_ignored_dates_cannot_complete_window(old_season: Season) -> None:
    """Successful HTTP responses must still honor the requested historical interval."""
    resource = seed(
        old_season,
        "dataservice",
        "window",
        "C1",
        start=date(2025, 5, 1),
        end=date(2025, 5, 31),
    )
    with pytest.raises(HistoryUnavailableError, match="date_filter_not_honored"):
        checkpoint(
            resource,
            {
                "rows": [{"wedstrijddatum": "2026-09-05T12:00:00+02:00"}],
                "wire_start": date(2025, 4, 28),
            },
        )
    resource.refresh_from_db()
    assert resource.state == "pending"


def dataservice_row() -> dict:
    """Fabricate the public result fields documented by Dataservice."""
    return {
        "wedstrijdcode": 1,
        "wedstrijddatum": "2025-05-10T13:30:00+0200",
        "thuisteamid": 1,
        "thuisteam": "Example 1",
        "thuisteamclubrelatiecode": "C1",
        "uitteamid": 2,
        "uitteam": "Example 2",
        "uitteamclubrelatiecode": "C2",
        "uitslag": "9 - 15",
    }


@pytest.mark.django_db
def test_dataservice_result_detail_pool_bulk_chain(old_season: Season) -> None:
    """Keep provider IDs distinct while publishing complete bulk results only once."""
    Club.objects.create(external_id="C1", name="Example")
    Club.objects.create(external_id="C2", name="Other")
    resource = seed(
        old_season,
        "dataservice",
        "window",
        "C1",
        start=date(2025, 5, 1),
        end=date(2025, 5, 31),
        sport="KORFBALL-VE-WK",
    )
    checkpoint(resource, {"rows": [dataservice_row()], "wire_start": date(2025, 4, 28)})
    detail = HistoricalResource.objects.get(kind="match")
    checkpoint(
        detail,
        {
            "wedstrijdinformatie": {
                "wedstrijddatetime": "2025-05-10T13:30:00+0200",
                "poulecode": 10,
                "thuisteamid": 1,
                "uitteamid": 2,
                "poule": "A",
                "klasse": "Example",
            }
        },
    )
    pool = HistoricalResource.objects.get(kind="pool")
    checkpoint(pool, {})
    pool_window = HistoricalResource.objects.get(kind="pool_window")
    checkpoint(
        pool_window, {"rows": [dataservice_row()], "wire_start": old_season.start_date}
    )
    standing = HistoricalResource.objects.get(kind="standing")
    checkpoint(
        standing,
        {
            "rows": [
                {
                    "teamnaam": f"Example {n}",
                    "clubrelatiecode": f"C{n}",
                    "gespeeldewedstrijden": 1,
                }
                for n in [1, 2]
            ]
        },
    )
    members = HistoricalResource.objects.get(kind="members")
    checkpoint(
        members,
        {
            "rows": [
                {"teamnaam": f"Example {n}", "clubrelatiecode": f"C{n}"} for n in [1, 2]
            ]
        },
    )
    reconcile_pool_coverage()
    pool.refresh_from_db()
    assert pool.coverage == "complete"
    assert Match.objects.count() == 1
    assert Match.objects.get().external_id == "ds:1"
    assert Match.objects.get().pool.external_id == "ds:10"
    assert not SyncResource.objects.exists()


@pytest.mark.django_db
def test_archive_publishes_distinct_score_attribution(old_season: Season) -> None:
    """Imported archive scores survive tracker reads without being labeled official."""
    row = old_match()
    for side in ["HomeTeam", "AwayTeam"]:
        club = row[side]["Club"]
        Club.objects.create(external_id=club["ClubId"], name=club["ClubName"])
    import_archive(
        old_season,
        {
            "namespace": "book",
            "source": "https://example.org/book.pdf",
            "matches": [row],
        },
    )
    publish_catalogue()
    match = Match.objects.get().local_match
    assert MatchData.objects.get(match_link=match).score_source == "archive"
    assert get_tracker_state(match, team=match.home_team)["score"] == {
        "for": 0,
        "against": 10,
    }


@pytest.mark.django_db
def test_known_old_poule_can_exceed_club_lookback(old_season: Season) -> None:
    """Do not impose the club-results 52-week limit on known poule results."""
    resource = seed(
        old_season,
        "dataservice",
        "pool_window",
        "10",
        start=date(2025, 1, 1),
        end=date(2025, 1, 31),
    )
    client = HistoryClient(dataservice_id="synthetic")
    client.session.get = Mock(return_value=Mock(status_code=200, headers={}, json=list))
    client.fetch(resource, Mock())
    assert client.session.get.call_args.kwargs["params"]["weekoffset"] < -LOOKBACK_WEEKS
