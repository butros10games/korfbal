"""Season-scoped app history: playing-season routing, discovery and match log."""

import csv
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.history import HistoryClient
from apps.competition.application.ports import FetchResult
from apps.competition.models import (
    HistoricalResource,
    Match,
    Pool,
    PoolEntry,
    SeasonBinding,
    SyncResource,
    TrafficState,
)
from apps.competition.services.history_editions import (
    current_edition,
    edition_log,
    edition_summary,
    prepare_edition,
    seed_edition,
    team_stratum,
)
from apps.competition.services.history_worker import current_work_due, run_history
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.seasons import INDOOR, OUTDOOR
from apps.competition.tasks import sync_competition_history
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_history import FakeClient
from apps.competition.tests.test_importer import team_payload
from apps.schedule.models import Season


EDITION = 2024


@pytest.fixture(autouse=True)
def no_spacing_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise durable reservations without sleeping in regression tests."""
    monkeypatch.setattr("apps.competition.services.traffic.time.sleep", lambda _: None)


def team(identifier: str, sport: str = OUTDOOR) -> dict:
    """Fabricate a team of one discipline."""
    return {**team_payload(identifier), "SportId": sport}


def row(
    identifier: str, when: str, *, sport: str = OUTDOOR, pool: int | None = 7
) -> dict:
    """Fabricate a scored final between T1 and T2."""
    return {
        "PublicMatchId": identifier,
        "MatchDateTime": when,
        "Status": "FINAL",
        "HomeTeam": team("T1", sport),
        "AwayTeam": team("T2", sport),
        "Pool": (
            {"PoolId": pool, "PoolName": "A", "ClassName": "Ereklasse"}
            if pool
            else None
        ),
        "HomeResult": {"Score": 20},
        "AwayResult": {"Score": 18},
        "AutoResult": None,
    }


def standing(played: int, *teams: str, sport: str = OUTDOOR) -> dict:
    """Official standings where every team played the same number of matches."""
    return {
        "PoolStandingTeam": [
            {**team(identifier, sport), "TotalMatches": played} for identifier in teams
        ]
    }


def full_year_pool() -> dict:
    """Fabricate an outdoor poule playing in autumn 2024 and spring 2025."""
    return {
        "ResultsFiltered": False,
        "MatchResult": [
            row("M1", "2024-09-14T15:00:00+0200"),
            row("M2", "2025-04-12T15:00:00+0200"),
        ],
        "PoolStanding": standing(2, "T1", "T2"),
    }


def run(replies: list) -> dict:
    """Run one unpublished history batch with synthetic replies."""
    client = FakeClient(replies)
    return run_history(lambda: client, budget=len(replies) or 1, publish_with=None)


def catalogue_team(identifier: str, sport: str = OUTDOOR) -> None:
    """Add one team to the current catalogue, the source of edition seeds."""
    current = Season.objects.create(
        name="current", start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)
    )
    Importer(current, timezone.now(), discover=False).team(team(identifier, sport))


def seed_edition_with_team(identifier: str, sport: str, *, scan: bool = False) -> dict:
    """Seed the edition from one fabricated catalogue team."""
    catalogue_team(identifier, sport)
    return seed_edition(EDITION, scan=scan)


@pytest.mark.django_db
def test_prepare_edition_creates_three_bound_playing_seasons() -> None:
    """Outdoor halves and indoor each get their own season and sport binding."""
    seasons = prepare_edition(EDITION)
    assert [seasons.autumn.name, seasons.indoor.name, seasons.spring.name] == [
        "Voor seizoen 2024",
        "Zaal seizoen 2024-2025",
        "Na seizoen 2025",
    ]
    bindings = {
        (row.scope.name, row.sport): row.season.name
        for row in SeasonBinding.objects.select_related("scope", "season")
    }
    assert bindings == {
        ("Voor seizoen 2024", OUTDOOR): "Voor seizoen 2024",
        ("Zaal seizoen 2024-2025", INDOOR): "Zaal seizoen 2024-2025",
        ("Na seizoen 2025", OUTDOOR): "Na seizoen 2025",
    }
    assert prepare_edition(EDITION).indoor.pk == seasons.indoor.pk


@pytest.mark.django_db
def test_prepare_edition_reuses_existing_season_names() -> None:
    """An existing indoor season keeps its identity and dates."""
    existing = Season.objects.create(
        name="zaal seizoen 2024-2025",
        start_date=date(2024, 10, 10),
        end_date=date(2025, 6, 1),
    )
    assert prepare_edition(EDITION).indoor.pk == existing.pk
    assert Season.objects.get(pk=existing.pk).end_date == date(2025, 6, 1)


@pytest.mark.django_db
def test_running_edition_is_not_historical() -> None:
    """The current edition is still synced by the live importer."""
    with pytest.raises(ValueError, match="finished"):
        prepare_edition(current_edition())


@pytest.mark.django_db
def test_seed_edition_queues_known_teams_once() -> None:
    """Catalogue teams become edition checkpoints; reseeding adds nothing."""
    summary = seed_edition_with_team("T1", OUTDOOR)
    assert summary["teams_queued"] == {f"{OUTDOOR}/senior": 1}
    assert seed_edition(EDITION, scan=False)["teams_queued"] == {f"{OUTDOOR}/senior": 0}
    resource = HistoricalResource.objects.get(kind="edition_team")
    assert (resource.source_id, resource.sport) == ("T1", OUTDOOR)
    assert resource.season.name == "Zaal seizoen 2024-2025"


@pytest.mark.django_db
def test_edition_requests_send_the_app_season_selector() -> None:
    """Old poules are empty without SeasonId; it is the anchor's start year."""
    seed_edition_with_team("T1", OUTDOOR)
    resource = HistoricalResource.objects.get(kind="edition_team")
    app = Mock(store=None)
    app._get.return_value = Mock(
        status_code=200, headers={}, json=Mock(return_value={"Pool": []})
    )
    HistoryClient(app).fetch(resource, Mock())
    params = app._get.call_args.kwargs["params"]
    assert params == {"PublicTeamId": "T1", "v": "2", "SeasonId": "2024"}
    assert app._get.call_args.args[0].endswith("team/TeamCompetitionData")


@pytest.mark.django_db
def test_full_year_outdoor_poule_is_split_into_both_halves() -> None:
    """Team -> poule discovery routes autumn and spring results separately."""
    seed_edition_with_team("T1", OUTDOOR)
    run([FetchResult(200, {"Pool": [{"PoolId": 7}]})])
    run([FetchResult(200, full_year_pool())])
    seasons = {
        match.external_id: match.season.name
        for match in Match.objects.select_related("season")
    }
    assert seasons == {"M1": "Voor seizoen 2024", "M2": "Na seizoen 2025"}
    pool = HistoricalResource.objects.get(kind="edition_pool")
    assert (pool.state, pool.coverage) == ("fetched", "complete")
    assert pool.evidence["imported"] == {"Voor seizoen 2024": 1, "Na seizoen 2025": 1}
    # Final standings belong to the half containing the poule's last match.
    standings = {
        entry.pool.season.name: entry.standing.get("TotalMatches")
        for entry in PoolEntry.objects.select_related("pool__season")
    }
    assert standings == {"Voor seizoen 2024": None, "Na seizoen 2025": 2}
    assert Pool.objects.count() == len(["autumn", "spring"])


@pytest.mark.django_db
def test_old_poule_teams_are_discovered_for_the_same_edition() -> None:
    """Teams that no longer exist are reached through old standings."""
    seed_edition_with_team("T1", OUTDOOR)
    run([FetchResult(200, {"Pool": [{"PoolId": 7}]})])
    data = full_year_pool()
    data["PoolStanding"] = standing(2, "T1", "T2", "T3")
    run([FetchResult(200, data)])
    queued = set(
        HistoricalResource.objects.filter(
            kind="edition_team", state="pending"
        ).values_list("source_id", flat=True)
    )
    assert queued == {"T2", "T3"}
    pool = HistoricalResource.objects.get(kind="edition_pool")
    # T3 played no recorded match, so the poule cannot prove completeness.
    assert pool.coverage == "partial"


@pytest.mark.django_db
def test_rows_outside_their_season_are_logged_as_skipped() -> None:
    """A narrow existing indoor season keeps its dates; outliers are logged."""
    Season.objects.create(
        name="Zaal seizoen 2024-2025",
        start_date=date(2024, 11, 1),
        end_date=date(2025, 3, 31),
    )
    seed_edition_with_team("T1", INDOOR)
    run([FetchResult(200, {"Pool": [{"PoolId": 7}]})])
    run([
        FetchResult(
            200,
            {
                "ResultsFiltered": False,
                "MatchResult": [
                    row("M1", "2024-11-09T15:00:00+0100", sport=INDOOR),
                    row("M2", "2025-04-26T15:00:00+0200", sport=INDOOR),
                ],
                "PoolStanding": standing(2, "T1", "T2", sport=INDOOR),
            },
        )
    ])
    assert list(Match.objects.values_list("external_id", flat=True)) == ["M1"]
    log = {entry["match_id"]: entry for entry in edition_log(EDITION)}
    assert log["M1"]["outcome"] == "imported"
    assert log["M1"]["season"] == "Zaal seizoen 2024-2025"
    assert (log["M2"]["outcome"], log["M2"]["reason"]) == (
        "skipped",
        "outside_season_dates",
    )
    assert HistoricalResource.objects.get(kind="edition_pool").coverage == "partial"


@pytest.mark.django_db
def test_team_without_season_data_is_recorded_empty() -> None:
    """Editions the provider no longer serves finish without inventing data."""
    seed_edition_with_team("T1", OUTDOOR)
    run([FetchResult(200, {"Pool": [], "UnboundMatchResults": {"MatchResult": []}})])
    resource = HistoricalResource.objects.get(kind="edition_team")
    assert (resource.state, resource.coverage, resource.reason) == (
        "fetched",
        "empty",
        "no_season_data",
    )
    assert edition_summary(EDITION)["discovery"]["edition_team"] == {"fetched/empty": 1}


@pytest.mark.django_db
def test_unbound_results_without_a_poule_are_imported_and_logged() -> None:
    """Results outside any poule are kept in their playing season."""
    seed_edition_with_team("T1", OUTDOOR)
    run([
        FetchResult(
            200,
            {
                "Pool": [],
                "UnboundMatchResults": {
                    "MatchResult": [row("M9", "2025-05-03T14:00:00+0200", pool=None)]
                },
            },
        )
    ])
    match = Match.objects.select_related("season").get()
    assert (match.external_id, match.season.name) == ("M9", "Na seizoen 2025")
    assert edition_summary(EDITION)["matches_imported"] == {"Na seizoen 2025": 1}


@pytest.mark.django_db
def test_play_off_poules_from_unbound_results_are_discovered() -> None:
    """Play-off poules are missing from Pool[]; their results reveal them."""
    seed_edition_with_team("T1", INDOOR)
    play_off = row("M5", "2025-03-25T20:30:00+0100", sport=INDOOR, pool=92861)
    run([
        FetchResult(
            200,
            {
                "Pool": [{"PoolId": 7}],
                "UnboundMatchResults": {"MatchResult": [play_off]},
            },
        )
    ])
    pools = set(
        HistoricalResource.objects.filter(kind="edition_pool").values_list(
            "source_id", flat=True
        )
    )
    assert pools == {"7", "92861"}
    # The play-off result is imported once, with its poule, not from the team.
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_publication_places_each_half_in_its_native_season() -> None:
    """Native matches land in the playing season their source scope binds to."""
    seed_edition_with_team("T1", OUTDOOR)
    run([FetchResult(200, {"Pool": [{"PoolId": 7}]})])
    run([FetchResult(200, full_year_pool())])
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    seasons = {
        match.external_id: match.local_match.season.name
        for match in Match.objects.select_related("local_match__season")
    }
    assert seasons == {"M1": "Voor seizoen 2024", "M2": "Na seizoen 2025"}


@pytest.mark.django_db
def test_command_queues_editions_and_writes_the_match_log(tmp_path: Path) -> None:
    """Operators queue editions, then export one CSV row per logged match."""
    catalogue_team("T1")
    out = StringIO()
    call_command(
        "import_competition_history", "edition", "--edition", "2024", stdout=out
    )
    assert f'"{OUTDOOR}/senior": 1' in out.getvalue()
    run([FetchResult(200, {"Pool": [{"PoolId": 7}]})])
    run([FetchResult(200, full_year_pool())])
    output = tmp_path / "log.csv"
    call_command(
        "import_competition_history",
        "log",
        "--edition",
        "2024",
        "--output",
        str(output),
        stdout=StringIO(),
    )
    rows = list(csv.DictReader(output.open(encoding="utf-8")))
    assert [(r["match_id"], r["season"], r["home_score"]) for r in rows] == [
        ("M1", "Voor seizoen 2024", "20"),
        ("M2", "Na seizoen 2025", "20"),
    ]
    status = StringIO()
    call_command(
        "import_competition_history", "status", "--edition", "2024", stdout=status
    )
    assert '"Na seizoen 2025": 1' in status.getvalue()


@pytest.mark.django_db
def test_history_task_waits_without_queued_work(settings: object) -> None:
    """The heartbeat opens no session until an edition has been queued."""
    settings.SPORTLINK_SYNC_ENABLED = True
    settings.SPORTLINK_SYNC_SESSION_FILE = "/missing/session.json"
    assert sync_competition_history() == {"status": "idle", "http_requests": 0}


@pytest.mark.django_db
def test_history_task_uses_the_history_pace(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued editions run in idle time at the configured history spacing."""
    settings.SPORTLINK_SYNC_ENABLED = True
    settings.SPORTLINK_SYNC_SESSION_FILE = "/private/session.json"
    settings.SPORTLINK_HISTORY_REQUEST_SPACING = 0
    seed_edition_with_team("T1", OUTDOOR)
    client = FakeClient([FetchResult(200, {"Pool": []})])
    monkeypatch.setattr(
        "apps.competition.tasks.scheduled_history_client", lambda: client
    )
    summary = sync_competition_history()
    assert (summary["status"], summary["http_requests"]) == ("ran", 1)
    assert TrafficState.objects.get().next_request_at <= timezone.now()


@pytest.mark.parametrize(
    ("name", "stratum"),
    [
        ("Example 1", "senior"),
        ("Achilles (A)/AKC MW1", "senior"),
        ("Nexus J5", "youth"),
        ("Example U19-1", "youth"),
        ("Example A1", "youth"),
    ],
)
def test_catalogue_teams_are_grouped_by_age(name: str, stratum: str) -> None:
    """Youth team IDs change every season, so youth seeds are probed separately."""
    assert team_stratum(name, OUTDOOR) == f"{OUTDOOR}/{stratum}"


def add_catalogue(teams: list[tuple[str, str]]) -> None:
    """Add named outdoor teams to the current catalogue."""
    current = Season.objects.create(
        name="current", start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)
    )
    importer = Importer(current, timezone.now(), discover=False)
    for identifier, name in teams:
        importer.team({**team(identifier), "TeamName": name})


class ById(FakeClient):
    """Answer each checkpoint by its provider identifier."""

    def __init__(self, replies: dict[str, dict]) -> None:
        """Keep one synthetic response per identifier."""
        super().__init__([])
        self.by_id = replies

    def fetch(self, resource: HistoricalResource, gate: object) -> FetchResult:
        """Return the identifier's response within the request budget."""
        gate.before_request()
        return FetchResult(200, self.by_id[resource.source_id])


def queued_teams() -> set[str]:
    """Return the edition team checkpoints that are waiting."""
    return set(
        HistoricalResource.objects.filter(
            kind="edition_team", state="pending"
        ).values_list("source_id", flat=True)
    )


@pytest.mark.django_db
def test_probe_with_data_releases_the_rest_of_its_stratum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strata cost one probe each; only data queues the remaining teams."""
    monkeypatch.setattr("apps.competition.services.history_editions.PROBE_SIZE", 1)
    add_catalogue([
        ("T1", "Example 1"),
        ("T2", "Example 2"),
        ("T3", "Example J1"),
        ("T4", "Example J2"),
    ])
    seed_edition(EDITION, scan=False)
    assert queued_teams() == {"T1", "T3"}
    replies = {
        "T1": {"Pool": [{"PoolId": 7}]},
        "T3": {"Pool": []},
        "7": {"ResultsFiltered": False, "MatchResult": [], "PoolStanding": None},
    }
    run_history(lambda: ById(replies), budget=3, publish_with=None)
    # The empty youth probe keeps the youth stratum closed; data opens seniors.
    assert queued_teams() == {"T2"}
    assert edition_summary(EDITION)["strata_released"] == [f"{OUTDOOR}/senior"]


@pytest.mark.django_db
def test_all_seeds_skips_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can still queue every catalogue team at once."""
    monkeypatch.setattr("apps.competition.services.history_editions.PROBE_SIZE", 1)
    add_catalogue([("T1", "Example J1"), ("T2", "Example J2")])
    seed_edition(EDITION, scan=False, all_seeds=True)
    assert queued_teams() == {"T1", "T2"}


@pytest.mark.django_db
def test_history_shares_the_lease_with_refresh_backlogs() -> None:
    """Periodic refreshes do not starve history; new discovery still wins."""
    current = Season.objects.create(
        name="active",
        start_date=timezone.localdate() - timedelta(days=30),
        end_date=timezone.localdate() + timedelta(days=30),
    )
    due = timezone.now() - timedelta(minutes=1)
    for kind in ("team_roster", "club_program", "pool_results"):
        SyncResource.objects.create(
            season=current, kind=kind, source_id="X", next_sync_at=due, fetched_at=due
        )
    assert not current_work_due(include_results=False)
    SyncResource.objects.create(
        season=current, kind="club_results", source_id="C9", next_sync_at=due
    )
    assert current_work_due(include_results=False)


class ScanClient(FakeClient):
    """Serve team probes and a poule-number range with data on some IDs."""

    def __init__(self, teams: dict[str, dict], pools_with_data: set[int]) -> None:
        """Keep team responses and the poule IDs that hold matches."""
        super().__init__([])
        self.teams = teams
        self.pools_with_data = pools_with_data
        self.requested: list[str] = []

    def fetch(self, resource: HistoricalResource, gate: object) -> FetchResult:
        """Answer teams by ID and poules with one match or nothing."""
        gate.before_request()
        self.requested.append(resource.source_id)
        if resource.kind == "edition_team":
            return FetchResult(200, self.teams[resource.source_id])
        pool = int(resource.source_id)
        if pool not in self.pools_with_data:
            return FetchResult(200, {"ResultsFiltered": False, "MatchResult": []})
        return FetchResult(
            200,
            {
                "ResultsFiltered": False,
                "MatchResult": [
                    row(f"M{pool}", "2024-11-09T15:00:00+0100", sport=INDOOR, pool=pool)
                ],
                "PoolStanding": standing(1, "T1", "T2", sport=INDOOR),
            },
        )


def scanned_pools() -> list[int]:
    """Return every poule ID queued for the edition."""
    return sorted(
        int(source_id)
        for source_id in HistoricalResource.objects.filter(
            kind="edition_pool"
        ).values_list("source_id", flat=True)
    )


@pytest.mark.django_db
def test_scan_reads_neighbouring_poules_and_stops_after_the_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe's poule seeds a window that grows with data and ends at a gap."""
    monkeypatch.setattr("apps.competition.services.history_editions.SCAN_MARGIN", 2)
    seed_edition_with_team("T1", INDOOR, scan=True)
    client = ScanClient({"T1": {"Pool": [{"PoolId": 10}]}}, {10, 11})
    run_history(lambda: client, budget=50, publish_with=None)
    assert scanned_pools() == list(range(8, 14))
    assert set(Match.objects.values_list("external_id", flat=True)) == {"M10", "M11"}
    # Poule responses already list every team: no team requests are queued.
    assert set(
        HistoricalResource.objects.filter(kind="edition_team").values_list(
            "source_id", flat=True
        )
    ) == {"T1"}
    assert edition_summary(EDITION)["scan_window"] == {"low": 8, "high": 13}


@pytest.mark.django_db
def test_scan_fills_the_gap_between_found_poules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Poules found far apart are joined into one contiguous window."""
    monkeypatch.setattr("apps.competition.services.history_editions.SCAN_MARGIN", 1)
    seed_edition_with_team("T1", INDOOR, scan=True)
    client = ScanClient({"T1": {"Pool": [{"PoolId": 10}, {"PoolId": 20}]}}, {10, 20})
    run_history(lambda: client, budget=50, publish_with=None)
    assert scanned_pools() == list(range(9, 22))
    assert client.requested.count("15") == 1


@pytest.mark.django_db
def test_scan_mode_probes_only_senior_teams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Youth teams cannot locate old poules; the scan reaches their poules."""
    monkeypatch.setattr("apps.competition.services.history_editions.PROBE_SIZE", 1)
    add_catalogue([("T1", "Example 1"), ("T3", "Example J1")])
    summary = seed_edition(EDITION)
    assert summary["mode"] == "scan"
    assert queued_teams() == {"T1"}
