"""The provider manager requests continuously while a pool publishes in parallel."""

from copy import deepcopy
from datetime import timedelta
import time
from unittest.mock import Mock, patch
import uuid

from django.utils import timezone
from korfbal.worker import supervised_commands
import pytest

from apps.competition.models import Match, SyncLease
from apps.competition.services import publication_worker
from apps.competition.services.importer import Importer
from apps.competition.services.provider_manager import (
    ERROR_SECONDS,
    IDLE_SECONDS,
    ManagerWiring,
    ProviderManager,
)
from apps.competition.services.publication_worker import (
    claim_publication,
    publish_backlog,
)
from apps.competition.services.publishing import pending_matches, publish_catalogue
from apps.competition.tasks import publish_competition_backlog
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload
from apps.competition.tests.test_provider_scheduler import (
    Recorder,
    history_pools,
    routine_feeds,
)
from apps.schedule.models import Season


@pytest.fixture(autouse=True)
def manager_mode(settings: object) -> None:
    """Run in the default manager mode with a configured private session."""
    settings.SPORTLINK_SCHEDULER = "manager"
    settings.SPORTLINK_SYNC_ENABLED = True
    settings.SPORTLINK_SYNC_SESSION_FILE = "/private/session.json"
    settings.SPORTLINK_REQUEST_SPACING = 1
    settings.SPORTLINK_HISTORY_REQUEST_SPACING = 0


@pytest.fixture
def live_season() -> Season:
    """Create an active live season."""
    return Season.objects.create(
        name="live",
        start_date=timezone.localdate() - timedelta(days=30),
        end_date=timezone.localdate() + timedelta(days=30),
    )


def import_fixtures(season: Season, count: int) -> None:
    """Import distinct pending source fixtures."""
    rows = []
    for index in range(count):
        row = deepcopy(match_payload())
        row["PublicMatchId"] = f"M{index + 1}"
        row["MatchDateTime"] = f"2026-09-{index + 5:02d}T13:30:00+0200"
        rows.append(row)
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": rows})


def sweep(season: Season | None = None) -> dict[str, object]:
    """Publish the backlog under a fresh publication lease."""
    owner = claim_publication()
    assert owner is not None
    return publish_backlog(
        schedule_changes=RecordingScheduleChanges(),
        live_season=season,
        owner=owner,
        deadline=time.monotonic() + 60,
    )


@pytest.mark.django_db
def test_backlog_is_published_in_chunked_sweeps(
    season: Season, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Short passes publish every pending fixture."""
    monkeypatch.setattr(publication_worker, "PUBLICATION_CHUNK", 2)
    import_fixtures(season, 5)
    result = sweep()
    assert not pending_matches().exists()
    assert result["passes"] >= len(["1-2", "3-4", "5"])
    assert not result["more"]


@pytest.mark.django_db
def test_unresolved_fixture_cannot_starve_the_sweep(
    season: Season, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixture that stays pending is retried once per sweep, not every pass."""
    monkeypatch.setattr(publication_worker, "PUBLICATION_CHUNK", 1)
    import_fixtures(season, 3)
    duplicate = deepcopy(match_payload())
    duplicate.update(
        PublicMatchId="M-duplicate", MatchDateTime="2026-09-05T13:30:00+0200"
    )
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [duplicate]}
    )
    result = sweep()
    assert list(pending_matches().values_list("external_id", flat=True)) == [
        "M-duplicate"
    ]
    assert not result["more"]


@pytest.mark.django_db
def test_publication_beside_an_import_ignores_the_provider_lease(
    season: Season,
) -> None:
    """The manager holds the provider lease; publication must not wait on it."""
    import_fixtures(season, 1)
    SyncLease.objects.create(
        key="sportlink",
        owner=uuid.uuid4(),
        expires_at=timezone.now() + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="import is running"):
        publish_catalogue(schedule_changes=RecordingScheduleChanges())
    publish_catalogue(
        schedule_changes=RecordingScheduleChanges(), alongside_import=True
    )
    match = Match.objects.get()
    # Published as of the change it read: a later import makes it pending again.
    assert match.published_at == match.updated_at
    assert not pending_matches().exists()


@pytest.mark.django_db
def test_publication_task_skips_while_another_pass_runs(season: Season) -> None:
    """Passes never overlap; a busy lease returns at once."""
    import_fixtures(season, 1)
    assert claim_publication() is not None
    assert publish_competition_backlog() == {"status": "busy"}
    assert pending_matches().exists()


@pytest.mark.django_db
def test_publication_task_requeues_an_unfinished_sweep(
    season: Season, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep cut off by its deadline continues in a new task."""
    import_fixtures(season, 2)
    monkeypatch.setattr("apps.competition.tasks.PUBLICATION_SECONDS", 0)
    with patch.object(publish_competition_backlog, "apply_async") as again:
        result = publish_competition_backlog()
    assert result["more"]
    again.assert_called_once()


def manager(recorder: Recorder, *, stop_after: int = 1) -> tuple[ProviderManager, Mock]:
    """Build a manager with fake clients that stops after some steps."""
    publication = Mock()
    steps = {"n": 0}

    def stopping() -> bool:
        steps["n"] += 1
        return steps["n"] > stop_after

    wiring = ManagerWiring(
        clients=recorder.clients,
        schedule_changes=RecordingScheduleChanges,
        request_publication=publication,
    )
    return ProviderManager(wiring, stopping=stopping, wait=Mock()), publication


@pytest.mark.django_db
def test_manager_idles_without_opening_a_session(season: Season) -> None:
    """No due feed and no history: wait, without credentials."""
    recorder = Recorder()
    clients = Mock(side_effect=recorder.clients)
    managed, publication = manager(recorder)
    managed.wiring = ManagerWiring(clients, RecordingScheduleChanges, publication)
    assert managed.step() == IDLE_SECONDS
    clients.assert_not_called()


@pytest.mark.django_db
def test_manager_turn_requests_publication_instead_of_publishing() -> None:
    """Requests never wait for publication; the publication pool is asked."""
    history_pools(2)
    recorder = Recorder()
    managed, publication = manager(recorder, stop_after=100)
    managed.step()
    assert [source for source, _ in recorder.log] == ["history", "history"]
    publication.assert_called_once()


@pytest.mark.django_db
def test_manager_runs_turns_back_to_back(settings: object, live_season: Season) -> None:
    """With work left at a turn's end, the next turn starts at once."""
    settings.SPORTLINK_SYNC_SEASON = live_season.name
    settings.SPORTLINK_REQUEST_SPACING = 60
    routine_feeds(live_season, 2)
    recorder = Recorder()
    managed, _ = manager(recorder)
    with patch("apps.competition.services.provider_manager.TURN_SECONDS", 1):
        assert managed.step() == 0


@pytest.mark.django_db
def test_failing_turn_never_stops_the_manager() -> None:
    """Errors are logged and retried after a pause; only a stop ends the loop."""
    history_pools(1)
    recorder = Recorder()
    managed, _ = manager(recorder, stop_after=2)
    with patch.object(ProviderManager, "step", side_effect=RuntimeError("boom")):
        managed.run_forever()
    assert managed.wait.call_args_list == [((ERROR_SECONDS,),), ((ERROR_SECONDS,),)]


@pytest.mark.parametrize(
    ("mode", "runs_manager"), [("manager", True), ("unified", False)]
)
def test_supervisor_starts_the_manager_only_in_manager_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, *, runs_manager: bool
) -> None:
    """The worker container runs the manager beside its Celery pools."""
    monkeypatch.setenv("SPORTLINK_SCHEDULER", mode)
    commands = supervised_commands()
    manager_command = ["python", "manage.py", "run_provider_manager"]
    assert (manager_command in commands) is runs_manager
