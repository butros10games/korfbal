"""The shared enrichment lane serves photos and club metadata off the live season."""

import base64
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from functools import partial
from io import StringIO
import json
from typing import Any, TypedDict, Unpack
from unittest.mock import MagicMock, Mock, patch

from django.core.management import call_command
from django.utils import timezone
import pytest
from pytest_django.fixtures import Settings

from apps.competition.adapters.outbound.logos import fetch_logo
from apps.competition.adapters.outbound.player_photos import fetch_photo
from apps.competition.adapters.outbound.sportlink import retry_delay
from apps.competition.application.ports import FetchResult, RequestGate
from apps.competition.models import Club, SyncLease, SyncResource, TrafficState
from apps.competition.services.catalog_metadata import (
    BATCH_SIZE,
    ENRICHMENT_KINDS,
    QUOTA_KEY,
    CatalogMetadataPlanner,
    enrichment_waiting,
)
from apps.competition.services.player_photos import (
    discover_photo,
    photo_name,
    settle_photo_siblings,
)
from apps.competition.services.polling import PollJob, PollPlanner
from apps.competition.services.provider_manager import (
    IDLE_SECONDS,
    ManagerWiring,
    ProviderManager,
    work_waiting,
)
from apps.competition.services.provider_scheduler import (
    ENRICHMENT,
    HISTORY,
    LIVE,
    ProviderTurn,
    TurnOptions,
    TurnState,
    choose,
)
from apps.competition.services.resources import MAX_FEED_FAILURES
from apps.competition.services.sync import checkpoint
from apps.competition.tasks import run_provider_turn
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_logos import BUCKET, DIGEST, image_payload
from apps.player.models import Player
from apps.schedule.models import Season


PNG = base64.b64decode(image_payload()["image"])
PHOTO_URL = f"https://binaries.sportlink.com/{BUCKET}/{DIGEST}"
PREVIEW_PEOPLE = ("eligible", "saved", "stale", "limited", "native", "exhausted")


@pytest.fixture(autouse=True)
def fast_traffic(settings: Settings) -> Iterator[None]:
    """Keep pacing small, never sleep and enable all enrichment kinds.

    Yields:
        None while the patch is active.

    """
    settings.SPORTLINK_REQUEST_SPACING = 1
    settings.SPORTLINK_HISTORY_REQUEST_SPACING = 0
    settings.SPORTLINK_ENRICHMENT_DAILY_LIMIT = 0
    settings.SPORTLINK_SYNC_SEASON = ""
    with patch("apps.competition.services.traffic.time.sleep"):
        yield


def past_seasons(count: int) -> list[Season]:
    """Create finished seasons, newest last."""
    return [
        Season.objects.create(
            name=f"{2018 + index}-{2019 + index}",
            start_date=date(2018 + index, 8, 1),
            end_date=date(2019 + index, 6, 30),
        )
        for index in range(count)
    ]


def imported_player(identifier: str = "P1", **values: object) -> Player:
    """Create a fresh, photo-permitting imported identity."""
    fields: dict[str, Any] = {
        "name": f"Example {identifier}",
        "knkv_person_id": identifier,
        "knkv_privacy": "NORMAL",
        "knkv_observed_at": timezone.now(),
        "knkv_photo": f"{BUCKET}/{DIGEST}",
    }
    fields.update(values)
    return Player.all_objects.create(**fields)


def queue_photo(player: Player, seasons: list[Season], **values: object) -> None:
    """Queue the person's photo in every season, as lineup imports do."""
    for season in seasons:
        SyncResource.objects.create(
            season=season,
            kind="player_photo",
            source_id=str(player.pk),
            next_sync_at=timezone.now() - timedelta(minutes=1),
            **values,
        )


class ImageClient:
    """Real photo/logo adapters over a fake binary transport."""

    def __init__(
        self, *, status: int = 200, during: Callable[[], None] | None = None
    ) -> None:
        """Answer every binary request with ``status``, running ``during`` first."""
        self.status = status
        self.during = during
        self.log: list[tuple[str, str]] = []

    def fetch(
        self, resource: SyncResource, gate: RequestGate | None = None
    ) -> FetchResult:
        """Dispatch images like the Sportlink client; answer programmes empty."""
        request = partial(self.get, gate=gate)
        if resource.kind == "player_photo":
            return fetch_photo(resource.source_id, gate, retry_delay, request=request)
        if resource.kind == "club_logo":
            return fetch_logo(resource.source_id, gate, retry_delay, request=request)
        assert gate is not None
        gate.before_request()
        self.log.append((LIVE, resource.kind))
        return FetchResult(200, {"ProgramItemMatchClub": []})

    def get(self, url: str, gate: RequestGate | None) -> MagicMock:
        """Reserve the request through the gate and return a fake image."""
        assert gate is not None
        gate.before_request()
        self.log.append((ENRICHMENT, url))
        if self.during is not None:
            self.during()
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = self.status
        response.headers = {"Retry-After": "120"}
        response.iter_content.return_value = [PNG]
        return response

    def close(self) -> None:
        """Nothing to release."""

    @property
    def images(self) -> list[str]:
        """Binary URLs requested, in order."""
        return [target for source, target in self.log if source == ENRICHMENT]


class TurnOverrides(TypedDict, total=False):
    """Typed optional provider-turn bounds and lane policy for the fake client."""

    request_seconds: float
    publish_seconds: float
    live_budget: int | None
    history_budget: int
    history_resource_ids: frozenset[int] | None
    history_share: float
    enrichment_budget: int
    enrichment_share: float
    publish: bool
    stop: Callable[[], bool] | None


def turn(
    client: ImageClient, season: Season | None = None, **options: Unpack[TurnOverrides]
) -> dict[str, Any]:
    """Run one provider turn with the enrichment lane enabled by default."""
    options.setdefault("enrichment_budget", 20)
    settings = TurnOptions(schedule_changes=RecordingScheduleChanges(), **options)
    return ProviderTurn(season, lambda: (client, Mock()), settings).run()


def requests(key: str) -> int:
    """Return the durable request counter for one traffic key."""
    state = TrafficState.objects.filter(key=key).first()
    return state.day_requests if state else 0


def unfetched(player: Player) -> int:
    """Count the person's photo rows still waiting."""
    return SyncResource.objects.filter(
        kind="player_photo", source_id=str(player.pk), fetched_at__isnull=True
    ).count()


@pytest.mark.parametrize(
    ("served", "share", "enrichment_share", "expected"),
    [
        ({LIVE: 0, HISTORY: 0, ENRICHMENT: 0}, 0.5, 0.25, HISTORY),
        ({LIVE: 0, HISTORY: 1, ENRICHMENT: 0}, 0.5, 0.25, LIVE),
        ({LIVE: 1, HISTORY: 1, ENRICHMENT: 0}, 0.5, 0.25, ENRICHMENT),
        ({LIVE: 2, HISTORY: 2, ENRICHMENT: 1}, 0.5, 0.25, HISTORY),
        ({LIVE: 0, HISTORY: 0, ENRICHMENT: 5}, 0.5, 0.0, HISTORY),
    ],
)
def test_enrichment_takes_its_weighted_share(
    served: dict[str, int], share: float, enrichment_share: float, expected: str
) -> None:
    """Routine work rotates by weight; ties favour history, then live."""
    state = TurnState(
        requests=dict(served), open={LIVE: True, HISTORY: True, ENRICHMENT: True}
    )
    assert choose(state, share, enrichment_share) == expected


def test_enrichment_alone_uses_every_slot() -> None:
    """A source with no weight still runs when nothing else is open."""
    state = TurnState(open={LIVE: False, HISTORY: False, ENRICHMENT: True})
    assert choose(state, 0.5, 0.0) == ENRICHMENT


@pytest.mark.django_db
def test_past_season_photo_downloads_without_a_live_season() -> None:
    """Off-season, the lane fetches through the shared gate and counts requests."""
    player = imported_player()
    queue_photo(player, past_seasons(1))
    client = ImageClient()
    result = turn(client)
    player.refresh_from_db()
    assert client.images == [PHOTO_URL]
    assert player.profile_picture.name == photo_name(player)
    assert unfetched(player) == 0
    assert result["enrichment_requests"] == 1
    assert requests("sportlink") == requests(QUOTA_KEY) == 1
    assert SyncLease.objects.get(key="sportlink").owner is None


@pytest.mark.django_db
def test_season_duplicates_make_one_request_and_settle() -> None:
    """Three seasons queue one person: one download completes every row."""
    player = imported_player()
    queue_photo(player, past_seasons(3))
    client = ImageClient()
    turn(client)
    assert client.images == [PHOTO_URL]
    assert unfetched(player) == 0


@pytest.mark.django_db
def test_saved_current_photo_settles_locally() -> None:
    """A person whose current image is already saved needs no request."""
    player = imported_player()
    queue_photo(player, past_seasons(2))
    Player.all_objects.filter(pk=player.pk).update(profile_picture=photo_name(player))
    client = ImageClient()
    result = turn(client)
    assert client.images == []
    assert unfetched(player) == 0
    enrichment = result["enrichment"]
    assert isinstance(enrichment, dict)
    assert enrichment["settled_locally"] == 1
    assert not enrichment_waiting(None, budget=20)


INELIGIBLE = {
    "stale": lambda now: {"knkv_observed_at": now - timedelta(days=9)},
    "limited": lambda _: {"knkv_privacy": "LIMITED"},
    "private": lambda _: {"knkv_privacy": "PRIVATE"},
    "archived": lambda now: {"archived_at": now},
    "native_upload": lambda _: {"profile_picture": "profile_pictures/manual.png"},
    "no_reference": lambda _: {"knkv_photo": ""},
}


@pytest.mark.django_db
@pytest.mark.parametrize("case", sorted(INELIGIBLE))
def test_ineligible_people_are_never_requested(case: str) -> None:
    """Stale, withdrawn, archived, native-upload or reference-less: no request."""
    player = imported_player(**INELIGIBLE[case](timezone.now()))
    queue_photo(player, past_seasons(2))
    assert not enrichment_waiting(None, budget=20)
    client = ImageClient()
    turn(client)
    assert client.images == []
    assert unfetched(player) == len(["2018", "2019"])


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case",
    ["private", "stale", "archived", "native_upload", "no_reference", "new_reference"],
)
def test_change_during_download_settles_no_sibling(case: str) -> None:
    """Discarded responses preserve every pending checkpoint and retry state."""
    player = imported_player()
    queue_photo(player, past_seasons(3), failures=2, last_error="http_500")
    before = list(SyncResource.objects.order_by("pk").values())
    change = (
        {"knkv_photo": f"{BUCKET}/ABC123"}
        if case == "new_reference"
        else INELIGIBLE[case](timezone.now())
    )

    def change_player() -> None:
        Player.all_objects.filter(pk=player.pk).update(**change)

    client = ImageClient(during=change_player)
    result = turn(client)
    player.refresh_from_db()
    assert client.images == [PHOTO_URL]
    assert player.profile_picture.name == change.get("profile_picture", "")
    assert unfetched(player) == len(["2018", "2019", "2020"])
    assert list(SyncResource.objects.order_by("pk").values()) == before
    assert result["enrichment"]["updated"] == 0
    assert result["enrichment"]["failed"] == 0
    assert result["enrichment"]["skipped"] == 1


@pytest.mark.django_db
def test_changed_reference_requeue_survives_the_old_download() -> None:
    """An old response cannot consume the attempted row's newly queued image."""
    player = imported_player()
    seasons = past_seasons(3)
    queue_photo(player, seasons, failures=2, last_error="old_reference_failure")
    requeued = []

    def change_player() -> None:
        discover_photo(player, {"Bucket": BUCKET, "Hash": "ABC123"}, seasons[-1])
        # Another attempt can set retry state for the new reference during I/O.
        SyncResource.objects.filter(season=seasons[-1]).update(
            failures=MAX_FEED_FAILURES,
            next_sync_at=timezone.now() + timedelta(hours=3),
            last_error="new_reference_failure",
        )
        requeued.extend(SyncResource.objects.order_by("pk").values())

    result = turn(ImageClient(during=change_player))
    assert list(SyncResource.objects.order_by("pk").values()) == requeued
    assert unfetched(player) == len(seasons)
    player.refresh_from_db()
    assert player.knkv_photo == f"{BUCKET}/ABC123"
    assert not player.profile_picture
    assert result["enrichment"]["updated"] == 0
    assert result["enrichment"]["skipped"] == 1


@pytest.mark.django_db
def test_conditional_photo_response_cannot_bypass_current_cache_proof() -> None:
    """Photo adapters need a named current image; a 304 alone proves nothing."""
    player = imported_player(knkv_privacy="PRIVATE")
    queue_photo(player, past_seasons(1), fetched_at=timezone.now(), failures=2)
    resource = SyncResource.objects.get()
    before = list(SyncResource.objects.values())
    with pytest.raises(ValueError, match="Unexpected conditional response"):
        checkpoint(resource, FetchResult(304), PollJob(resource, set(), 3))
    assert list(SyncResource.objects.values()) == before


def saved_backlog(count: int) -> None:
    """Queue distinct cached people to exercise real local settlement reads."""
    seasons = past_seasons(1)
    for index in range(count):
        player = imported_player(f"saved-{index}")
        Player.all_objects.filter(pk=player.pk).update(
            profile_picture=photo_name(player)
        )
        queue_photo(player, seasons)


@pytest.mark.django_db
def test_lane_availability_does_not_settle_cached_people() -> None:
    """Planning cached work is read-only, even with more than a turn's batch."""
    saved_backlog(BATCH_SIZE + 3)
    before = list(SyncResource.objects.order_by("pk").values())
    planner = CatalogMetadataPlanner(None, timezone.now())
    for _ in range(3):
        assert planner.available()
    assert list(SyncResource.objects.order_by("pk").values()) == before
    assert planner.summary["settled_locally"] == 0
    assert planner.summary["identities_loaded"] <= BATCH_SIZE


@pytest.mark.django_db
def test_cached_backlog_is_bounded_per_turn() -> None:
    """No provider traffic, and at most 50 identities settled in one turn."""
    remaining = 3
    saved_backlog(BATCH_SIZE + remaining)
    client = ImageClient()
    result = turn(client)
    assert client.images == []
    assert result["enrichment"]["settled_locally"] == BATCH_SIZE
    assert result["enrichment"]["rows_settled"] == BATCH_SIZE
    assert result["enrichment"]["identities_loaded"] == BATCH_SIZE
    assert result["enrichment"]["batch_limit_reached"] == 1
    assert result["enrichment"]["requests"] == 0
    assert result["enrichment_requests"] == 0
    assert result["turn_requests"][ENRICHMENT] == 0
    assert SyncResource.objects.filter(fetched_at__isnull=True).count() == remaining
    assert enrichment_waiting(None, budget=20)
    assert not TrafficState.objects.exists()
    # The next normal manager turn can finish the remaining bounded work.
    assert turn(client)["enrichment"]["settled_locally"] == remaining


@pytest.mark.django_db
def test_cached_settlements_yield_to_the_turn_deadline() -> None:
    """Crossing the request deadline during local work leaves the rest pending."""
    saved_backlog(3)
    clock = {"seconds": 0.0}

    def settle_then_expire(source_id: str, now: datetime) -> int:
        count = settle_photo_siblings(source_id, now)
        clock["seconds"] = 2.0
        return count

    with (
        patch(
            "apps.competition.services.provider_scheduler.time.monotonic",
            lambda: clock["seconds"],
        ),
        patch(
            "apps.competition.services.catalog_metadata.settle_photo_siblings",
            settle_then_expire,
        ),
    ):
        result = turn(ImageClient(), request_seconds=1)
    assert result["enrichment"]["settled_locally"] == 1
    assert SyncResource.objects.filter(fetched_at__isnull=True).count() == len([
        "second",
        "third",
    ])
    assert result["more_work"]


@pytest.mark.django_db
def test_newly_urgent_live_feed_interrupts_cached_settlements() -> None:
    """A programme becoming due runs between two cached-photo settlements."""
    now = timezone.now()
    live = Season.objects.create(
        name="live",
        start_date=timezone.localdate() - timedelta(days=30),
        end_date=timezone.localdate() + timedelta(days=30),
    )
    SyncResource.objects.create(
        season=live,
        kind="clubs",
        fetched_at=now,
        next_sync_at=now + timedelta(days=1),
    )
    SyncResource.objects.create(
        season=live,
        kind="club_program",
        source_id="soon-due",
        next_sync_at=now + timedelta(seconds=10),
    )
    saved_backlog(3)
    clock = {"seconds": 0.0}
    trace = []
    client = ImageClient()
    fetch = client.fetch

    def settle_then_advance(source_id: str, observed_at: datetime) -> int:
        trace.append("local_photo")
        count = settle_photo_siblings(source_id, observed_at)
        clock["seconds"] = 12.0
        return count

    def record_fetch(resource: SyncResource, gate: RequestGate | None) -> FetchResult:
        trace.append(resource.kind)
        return fetch(resource, gate)

    with (
        patch(
            "apps.competition.services.provider_scheduler.time.monotonic",
            lambda: clock["seconds"],
        ),
        patch(
            "django.utils.timezone.now",
            lambda: now + timedelta(seconds=clock["seconds"]),
        ),
        patch(
            "apps.competition.services.catalog_metadata.settle_photo_siblings",
            settle_then_advance,
        ),
        patch.object(client, "fetch", record_fetch),
    ):
        result = turn(client, live, history_share=1, enrichment_share=1, publish=False)
    assert trace == ["local_photo", "club_program", "local_photo", "local_photo"]
    assert result["enrichment"]["settled_locally"] == len(["first", "second", "third"])
    assert requests("sportlink") == 1
    assert requests(QUOTA_KEY) == 0


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ENRICHMENT_KINDS)
def test_never_fetched_live_enrichment_is_routine(season: Season, kind: str) -> None:
    """Unfetched photos and club metadata cannot preempt history as urgent."""
    SyncResource.objects.create(
        season=season, kind=kind, source_id="routine", next_sync_at=timezone.now()
    )
    planner = PollPlanner(season, timezone.now())
    assert planner.candidate_jobs()
    assert not planner.urgent_due()


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["club_program", "team_roster"])
def test_never_fetched_live_competition_feed_stays_urgent(
    season: Season, kind: str
) -> None:
    """Schedule discovery and first roster observations keep their priority."""
    SyncResource.objects.create(
        season=season, kind=kind, source_id="competition", next_sync_at=timezone.now()
    )
    assert PollPlanner(season, timezone.now()).urgent_due()


@pytest.mark.django_db(transaction=True)
def test_manager_requests_publication_for_enrichment_updates(
    settings: Settings,
) -> None:
    """An enrichment-only successful turn schedules native publication."""
    settings.SPORTLINK_SYNC_ENABLED = True
    settings.SPORTLINK_SYNC_SESSION_FILE = "/private/session.json"
    settings.SPORTLINK_ENRICHMENT_MAX_REQUESTS = 20
    queue_photo(imported_player(), past_seasons(1))
    client = ImageClient()
    publication = Mock()
    manager = ProviderManager(
        ManagerWiring(lambda: (client, Mock()), RecordingScheduleChanges, publication),
        stopping=lambda: False,
        wait=Mock(),
    )
    assert manager.step() == IDLE_SECONDS
    assert client.images == [PHOTO_URL]
    publication.assert_called_once()


@pytest.mark.django_db
def test_failure_backoff_holds_every_sibling() -> None:
    """Another season cannot bypass a person's backoff or failure ceiling."""
    player = imported_player()
    queue_photo(player, past_seasons(3))
    turn(ImageClient(status=500))
    failing = SyncResource.objects.get(failures=1)
    assert failing.last_error == "http_500"
    assert unfetched(player) == len(["2018", "2019", "2020"])
    assert not enrichment_waiting(None, budget=20)
    client = ImageClient(status=500)
    turn(client)
    assert client.images == []
    # Once the backoff ends, the same row continues its streak.
    SyncResource.objects.filter(pk=failing.pk).update(next_sync_at=timezone.now())
    turn(ImageClient(status=500))
    failing.refresh_from_db()
    assert failing.failures == len(["first", "second"])
    assert SyncResource.objects.filter(failures=0).count() == len(["2019", "2020"])
    SyncResource.objects.filter(pk=failing.pk).update(
        failures=MAX_FEED_FAILURES, next_sync_at=timezone.now()
    )
    assert not enrichment_waiting(None, budget=20)


@pytest.mark.django_db
def test_new_reference_clears_the_old_failure_streak() -> None:
    """Retry state belongs to a person and reference, not to a season row."""
    player = imported_player()
    first, second = past_seasons(2)
    queue_photo(player, [first], failures=MAX_FEED_FAILURES)
    discover_photo(player, {"Bucket": BUCKET, "Hash": "ABC123"}, second)
    assert not SyncResource.objects.filter(failures__gt=0).exists()
    assert enrichment_waiting(None, budget=20)


@pytest.mark.django_db
def test_live_lane_owns_its_pending_photo_and_settles_history() -> None:
    """The live season fetches its own row; past-season siblings follow."""
    live = Season.objects.create(
        name="live",
        start_date=timezone.localdate() - timedelta(days=30),
        end_date=timezone.localdate() + timedelta(days=30),
    )
    SyncResource.objects.create(
        season=live,
        kind="clubs",
        fetched_at=timezone.now(),
        next_sync_at=timezone.now() + timedelta(days=1),
    )
    player = imported_player()
    queue_photo(player, [*past_seasons(2), live])
    assert not enrichment_waiting(live, budget=20)
    client = ImageClient()
    result = turn(client, live)
    assert client.images == [PHOTO_URL]
    assert result["enrichment_requests"] == 0
    assert unfetched(player) == 0


@pytest.mark.django_db
def test_urgent_live_work_preempts_enrichment() -> None:
    """A never-fetched live feed runs first, however large the lane's weight."""
    live = Season.objects.create(
        name="live",
        start_date=timezone.localdate() - timedelta(days=30),
        end_date=timezone.localdate() + timedelta(days=30),
    )
    SyncResource.objects.create(
        season=live,
        kind="clubs",
        fetched_at=timezone.now(),
        next_sync_at=timezone.now() + timedelta(days=1),
    )
    SyncResource.objects.create(
        season=live, kind="club_program", source_id="C1", next_sync_at=timezone.now()
    )
    queue_photo(imported_player(), past_seasons(1))
    client = ImageClient()
    # Live has no routine weight here; only its urgency puts it first.
    turn(client, live, history_share=1.0, enrichment_share=1.0)
    assert [source for source, _ in client.log] == [LIVE, ENRICHMENT]


@pytest.mark.django_db
def test_rate_limited_image_cools_down_the_account() -> None:
    """A 429 keeps its Retry-After and ends the whole provider turn."""
    seasons = past_seasons(1)
    for identifier in ("P1", "P2"):
        queue_photo(imported_player(identifier), seasons)
    client = ImageClient(status=429)
    turn(client)
    assert len(client.images) == 1
    assert SyncResource.objects.get(failures=1).last_error == "http_429"
    lease = SyncLease.objects.get(key="sportlink")
    assert lease.expires_at >= timezone.now() + timedelta(seconds=100)


@pytest.mark.django_db
def test_turn_and_daily_caps_close_only_the_lane(settings: Settings) -> None:
    """Per-turn and durable daily caps bound the lane's requests."""
    seasons = past_seasons(1)
    for identifier in ("P1", "P2", "P3"):
        queue_photo(imported_player(identifier), seasons)
    client = ImageClient()
    result = turn(client, enrichment_budget=2)
    assert len(client.images) == len(["P1", "P2"])
    assert not result["more_work"]
    # The durable daily cap stops the lane mid-turn without a failure.
    settings.SPORTLINK_ENRICHMENT_DAILY_LIMIT = len(["P1", "P2", "P3"])
    late = imported_player("P4")
    queue_photo(late, seasons)
    client = ImageClient()
    result = turn(client)
    assert len(client.images) == 1
    assert requests(QUOTA_KEY) == len(["P1", "P2", "P3"])
    assert unfetched(late) == 1
    assert not SyncResource.objects.filter(failures__gt=0).exists()
    enrichment = result["enrichment"]
    assert isinstance(enrichment, dict)
    assert enrichment["deferred"] == 1
    assert not enrichment_waiting(None, budget=20)


@pytest.mark.django_db
def test_lane_is_closed_by_default_and_in_scoped_history_drains() -> None:
    """No budget, no requests; a reviewed history drain spends nothing here."""
    queue_photo(imported_player(), past_seasons(1))
    assert not enrichment_waiting(None, budget=0)
    client = ImageClient()
    turn(client, enrichment_budget=0)
    turn(client, history_resource_ids=frozenset())
    assert client.images == []


@pytest.mark.django_db
def test_club_logo_identity_is_requested_once_across_seasons() -> None:
    """Club rows dedup by kind and club; a live-season row stays with live."""
    club = Club.objects.create(
        external_id="logo-club", name="Logo Club", logo_bucket=BUCKET, logo_hash=DIGEST
    )
    seasons = past_seasons(2)
    for season in seasons:
        SyncResource.objects.create(
            season=season,
            kind="club_logo",
            source_id=club.external_id,
            next_sync_at=timezone.now() - timedelta(minutes=1),
        )
    client = ImageClient()
    turn(client)
    club.refresh_from_db()
    assert client.images == [PHOTO_URL]
    assert club.cached_logo
    assert not SyncResource.objects.filter(fetched_at__isnull=True).exists()
    live = Season.objects.create(
        name="live",
        start_date=timezone.localdate() - timedelta(days=30),
        end_date=timezone.localdate() + timedelta(days=30),
    )
    SyncResource.objects.create(
        season=live,
        kind="club_logo",
        source_id=club.external_id,
        next_sync_at=timezone.now(),
    )
    SyncResource.objects.filter(season__in=seasons).update(next_sync_at=timezone.now())
    assert not enrichment_waiting(live, budget=20)


@pytest.mark.django_db
def test_turn_task_and_manager_start_for_enrichment_alone(settings: Settings) -> None:
    """Without a live season or history, enabled enrichment still starts a turn."""
    settings.SPORTLINK_SYNC_ENABLED = True
    settings.SPORTLINK_SYNC_SESSION_FILE = "/private/session.json"
    settings.SPORTLINK_SCHEDULER = "unified"
    settings.SPORTLINK_ENRICHMENT_MAX_REQUESTS = 0
    queue_photo(imported_player(), past_seasons(1))
    with patch("apps.competition.tasks.provider_clients") as clients:
        assert run_provider_turn()["status"] == "idle"
    clients.assert_not_called()
    assert not work_waiting(None)
    settings.SPORTLINK_ENRICHMENT_MAX_REQUESTS = 5
    assert work_waiting(None)
    client = ImageClient()
    with (
        patch("apps.competition.tasks.provider_clients", lambda: (client, Mock())),
        patch.object(run_provider_turn, "apply_async"),
    ):
        result = run_provider_turn()
    assert result["enrichment_requests"] == 1
    assert client.images == [PHOTO_URL]


@pytest.mark.django_db
def test_preview_counts_distinct_people_without_writes(settings: Settings) -> None:
    """The preview classifies each person once and changes nothing."""
    live = Season.objects.create(
        name="live",
        start_date=timezone.localdate() - timedelta(days=30),
        end_date=timezone.localdate() + timedelta(days=30),
    )
    settings.SPORTLINK_SYNC_SEASON = live.name
    seasons = past_seasons(len(["2018", "2019"]))
    queue_photo(imported_player("eligible"), seasons)
    saved = imported_player("saved")
    Player.all_objects.filter(pk=saved.pk).update(profile_picture=photo_name(saved))
    queue_photo(saved, seasons)
    queue_photo(
        imported_player("stale", knkv_observed_at=timezone.now() - timedelta(days=9)),
        seasons,
    )
    queue_photo(imported_player("limited", knkv_privacy="LIMITED"), seasons)
    queue_photo(
        imported_player("native", profile_picture="profile_pictures/manual.png"),
        seasons,
    )
    queue_photo(imported_player("exhausted"), seasons, failures=MAX_FEED_FAILURES)
    queue_photo(imported_player("live"), [*seasons, live])
    before = list(SyncResource.objects.order_by("pk").values())
    players = list(Player.all_objects.order_by("pk").values())
    stdout = StringIO()
    call_command("preview_historical_photos", stdout=stdout)
    report = json.loads(stdout.getvalue())
    queued = [*PREVIEW_PEOPLE, "live"]
    assert report["queued_people"] == len(queued)
    assert report["queued_rows"] == len(queued) * len(seasons)
    assert report["request_estimate"] == 1
    assert report["local_settlement_rows"] == len(seasons)
    assert report["outcomes"] == {
        "live_owned": 1,
        "exhausted": 1,
        "backing_off": 0,
        "not_due": 0,
        "missing_player": 0,
        "archived": 0,
        "hidden_stale_or_withdrawn": 1,
        "privacy_not_permitted": 1,
        "no_reference": 0,
        "native_upload": 1,
        "already_saved": 1,
        "eligible": 1,
    }
    assert report["eligible_freshness"]["3_to_8_days"] == 1
    assert report["http_requests"] == 0
    assert list(SyncResource.objects.order_by("pk").values()) == before
    assert list(Player.all_objects.order_by("pk").values()) == players
    assert not TrafficState.objects.exists()
