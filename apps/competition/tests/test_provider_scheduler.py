"""One provider turn schedules live and history requests under one lease."""

from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import Mock, patch

from django.utils import timezone
import pytest

from apps.competition.application.ports import FetchResult, ProviderCooldownError
from apps.competition.models import HistoricalResource, SyncLease, SyncResource
from apps.competition.services.history import HistoryUnavailableError
from apps.competition.services.history_editions import prepare_edition, seed_many
from apps.competition.services.provider_scheduler import (
    HISTORY,
    LIVE,
    ProviderTurn,
    TurnOptions,
    TurnState,
    choose,
)
from apps.competition.services.traffic import TrafficGate
from apps.competition.tasks import (
    run_provider_turn,
    sync_competition_history,
    sync_current_competition,
)
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.schedule.models import Season


@pytest.fixture(autouse=True)
def fast_traffic(settings: object) -> Iterator[None]:
    """Keep pacing small and never sleep.

    Yields:
        None while the patch is active.

    """
    settings.SPORTLINK_REQUEST_SPACING = 1
    settings.SPORTLINK_HISTORY_REQUEST_SPACING = 0
    with patch("apps.competition.services.traffic.time.sleep"):
        yield


@pytest.fixture
def live_season() -> Season:
    """Create an active live season."""
    return Season.objects.create(
        name="live",
        start_date=timezone.localdate() - timedelta(days=30),
        end_date=timezone.localdate() + timedelta(days=30),
    )


def routine_feeds(season: Season, count: int, *, fetched: bool = True) -> None:
    """Queue due club programmes; fetched ones are routine refreshes.

    The season's club catalogue is already fetched and not due.
    """
    past = timezone.now() - timedelta(days=2)
    SyncResource.objects.get_or_create(
        season=season,
        kind="clubs",
        source_id="",
        defaults={
            "fetched_at": past,
            "next_sync_at": timezone.now() + timedelta(days=1),
        },
    )
    for index in range(count):
        SyncResource.objects.create(
            season=season,
            kind="club_program",
            source_id=f"C{index}",
            next_sync_at=past,
            fetched_at=past if fetched else None,
        )


def history_pools(count: int) -> None:
    """Queue historical poules for edition 2024."""
    anchor = prepare_edition(2024).indoor
    seed_many(
        anchor,
        "edition_pool",
        {str(index): "" for index in range(1, count + 1)},
        parent=None,
        reference="test",
    )


class Recorder:
    """Fake live and history clients that log the order of provider requests."""

    def __init__(self, history_error: Exception | None = None) -> None:
        """Start with an empty request log."""
        self.log: list[tuple[str, str]] = []
        self.history_error = history_error
        self.live = Mock()
        self.live.fetch.side_effect = self.fetch_live
        self.history = Mock()
        self.history.fetch.side_effect = self.fetch_history

    def fetch_live(self, resource: SyncResource, gate: TrafficGate) -> FetchResult:
        """Answer a club programme request."""
        gate.before_request()
        self.log.append((LIVE, resource.source_id))
        return FetchResult(200, {"ProgramItemMatchClub": []})

    def fetch_history(
        self, resource: HistoricalResource, gate: TrafficGate
    ) -> FetchResult:
        """Answer an empty historical poule, or raise the configured error."""
        gate.before_request()
        self.log.append((HISTORY, resource.source_id))
        if self.history_error is not None:
            raise self.history_error
        return FetchResult(200, {"ResultsFiltered": False, "MatchResult": []})

    def clients(self) -> tuple[Mock, Mock]:
        """Return both fake clients."""
        return self.live, self.history


def turn(
    season: Season | None, recorder: Recorder, **options: object
) -> dict[str, object]:
    """Run one provider turn with fake clients."""
    settings = TurnOptions(schedule_changes=RecordingScheduleChanges(), **options)
    return ProviderTurn(season, recorder.clients, settings).run()


@pytest.mark.parametrize(
    ("served", "share", "expected"),
    [
        ({LIVE: 0, HISTORY: 0}, 0.5, HISTORY),
        ({LIVE: 0, HISTORY: 1}, 0.5, LIVE),
        ({LIVE: 1, HISTORY: 1}, 0.5, HISTORY),
        ({LIVE: 3, HISTORY: 1}, 0.25, HISTORY),
        ({LIVE: 2, HISTORY: 1}, 0.25, LIVE),
        ({LIVE: 0, HISTORY: 0}, 0, LIVE),
        ({LIVE: 0, HISTORY: 0}, 1, HISTORY),
    ],
)
def test_share_picks_the_source_furthest_behind(
    served: dict[str, int], share: float, expected: str
) -> None:
    """Non-urgent requests follow the configured history share."""
    assert choose(TurnState(requests=dict(served)), share) == expected


def test_an_idle_source_gives_its_turn_away() -> None:
    """History uses spare capacity, and live gets everything without history."""
    state = TurnState(open={LIVE: False, HISTORY: True})
    assert choose(state, 0.0) == HISTORY
    state.open = {LIVE: True, HISTORY: False}
    assert choose(state, 1.0) == LIVE
    state.open = {LIVE: False, HISTORY: False}
    assert choose(state, 0.5) is None


@pytest.mark.django_db
def test_turn_alternates_routine_live_work_and_history(live_season: Season) -> None:
    """One lease, both sources, alternating by the default share."""
    routine_feeds(live_season, 2)
    history_pools(2)
    recorder = Recorder()
    result = turn(live_season, recorder)
    assert [source for source, _ in recorder.log] == [HISTORY, LIVE, HISTORY, LIVE]
    assert result["turn_requests"] == {LIVE: 2, HISTORY: 2}
    assert SyncLease.objects.get(key="sportlink").owner is None
    assert not result["more_work"]


@pytest.mark.django_db
def test_urgent_live_work_preempts_history(live_season: Season) -> None:
    """A never-fetched live feed runs before history whatever the share says."""
    routine_feeds(live_season, 1, fetched=False)
    history_pools(1)
    recorder = Recorder()
    turn(live_season, recorder, history_share=1.0)
    assert recorder.log[0][0] == LIVE
    assert [source for source, _ in recorder.log] == [LIVE, HISTORY]


@pytest.mark.django_db
def test_due_match_form_ends_the_turn(
    live_season: Season, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private form actions get the lease before any batch request."""
    routine_feeds(live_season, 1)
    history_pools(1)
    monkeypatch.setattr(
        "apps.competition.services.provider_scheduler.match_forms_due", lambda: True
    )
    recorder = Recorder()
    result = turn(live_season, recorder)
    assert recorder.log == []
    assert result["deferred"] == 1


@pytest.mark.django_db
def test_history_rate_limit_stops_the_whole_turn(live_season: Season) -> None:
    """Live and history share one account: a 429 cools both down."""
    routine_feeds(live_season, 2)
    history_pools(1)
    recorder = Recorder(history_error=ProviderCooldownError(120))
    turn(live_season, recorder)
    assert recorder.log == [(HISTORY, "1")]
    lease = SyncLease.objects.get(key="sportlink")
    assert lease.expires_at >= timezone.now() + timedelta(seconds=100)


@pytest.mark.django_db
def test_spent_history_budget_leaves_live_work_running(live_season: Season) -> None:
    """A per-source cap closes only that source for the rest of the turn."""
    routine_feeds(live_season, 3)
    history_pools(3)
    recorder = Recorder()
    result = turn(live_season, recorder, history_budget=1)
    assert result["turn_requests"][LIVE] == len(["C0", "C1", "C2"])
    assert [source for source, _ in recorder.log].count(HISTORY) == 1


@pytest.mark.django_db
def test_history_runs_without_an_active_live_season() -> None:
    """Off-season, the turn serves history alone."""
    history_pools(2)
    recorder = Recorder()
    result = turn(None, recorder)
    assert [source for source, _ in recorder.log] == [HISTORY, HISTORY]
    assert result["turn_requests"] == {LIVE: 0, HISTORY: 2}


@pytest.mark.django_db
def test_request_window_end_requests_an_immediate_next_turn(
    live_season: Season,
) -> None:
    """Open work at the deadline chains the next turn instead of idling."""
    routine_feeds(live_season, 2)
    recorder = Recorder()
    result = turn(live_season, recorder, request_seconds=0)
    assert recorder.log == []
    assert result["more_work"]


@pytest.mark.django_db
def test_task_runs_the_unified_turn_and_chains_more_work(
    settings: object, live_season: Season
) -> None:
    """The beat task opens clients only after the lease and chains open work."""
    settings.SPORTLINK_SYNC_ENABLED = True
    settings.SPORTLINK_SYNC_SEASON = live_season.name
    settings.SPORTLINK_SYNC_SESSION_FILE = "/private/session.json"
    settings.SPORTLINK_SCHEDULER = "unified"
    routine_feeds(live_season, 1)
    history_pools(1)
    recorder = Recorder()
    with (
        patch("apps.competition.tasks.provider_clients", recorder.clients),
        patch.object(run_provider_turn, "apply_async") as chain,
    ):
        result = run_provider_turn()
    assert result["turn_requests"] == {LIVE: 1, HISTORY: 1}
    chain.assert_not_called()


@pytest.mark.django_db
def test_task_is_idle_without_work(settings: object, live_season: Season) -> None:
    """No due feed and no history: no lease, no session."""
    settings.SPORTLINK_SYNC_ENABLED = True
    settings.SPORTLINK_SYNC_SEASON = live_season.name
    settings.SPORTLINK_SYNC_SESSION_FILE = "/private/session.json"
    settings.SPORTLINK_SCHEDULER = "unified"
    routine_feeds(live_season, 0)
    with patch("apps.competition.tasks.provider_clients") as clients:
        assert run_provider_turn()["status"] == "idle"
    clients.assert_not_called()


@pytest.mark.django_db
def test_scheduler_switch_selects_exactly_one_path(settings: object) -> None:
    """Unified mode silences the legacy tasks; legacy mode silences the turn."""
    settings.SPORTLINK_SYNC_ENABLED = True
    settings.SPORTLINK_SYNC_SESSION_FILE = "/private/session.json"
    settings.SPORTLINK_SCHEDULER = "unified"
    assert sync_current_competition()["status"] == "scheduler_unified"
    assert sync_competition_history()["status"] == "scheduler_unified"
    settings.SPORTLINK_SCHEDULER = "legacy"
    assert run_provider_turn()["status"] == "scheduler_legacy"


@pytest.mark.django_db
def test_history_source_access_problems_leave_live_work_running(
    live_season: Season,
) -> None:
    """Only account-wide failures stop the turn; a history 403 closes history."""
    routine_feeds(live_season, 2)
    history_pools(2)
    recorder = Recorder(history_error=HistoryUnavailableError("access_denied"))
    result = turn(live_season, recorder)
    assert result["turn_requests"] == {LIVE: 2, HISTORY: 1}
    assert SyncLease.objects.get(key="sportlink").expires_at <= timezone.now()
