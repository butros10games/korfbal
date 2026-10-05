"""One provider turn: all Sportlink batch traffic under one lease and one policy.

The live sync and season history used to claim the provider lease in separate
multi-minute turns, so priority only applied between turns and their share of
the account depended on task timing. A turn now owns the lease once and picks
every request itself:

1. Time-critical live work (fresh results, schedules near kickoff, never-fetched
   feeds, rosters near the visibility deadline) always goes first, checked
   before every request.
2. Otherwise routine live refreshes, history and the opt-in enrichment lane
   (player photos and club metadata outside the live season) share requests
   by explicit weights.

Private match-form actions keep their own worker and end a turn when they are due.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import time
import uuid

from django.conf import settings
from django.utils import timezone

from apps.competition.application.ports import CompetitionClient, HistoricalClient
from apps.competition.models import HistoricalResource, SyncLease
from apps.competition.services.catalog_metadata import (
    CatalogMetadataPlanner,
    EnrichmentGate,
    enrichment_waiting,
)
from apps.competition.services.history_worker import (
    PUBLISH_BACKLOG,
    HistoryBatch,
    history_spacing,
    next_resource,
)
from apps.competition.services.importer import enqueue
from apps.competition.services.monitoring import bind_run_lease, progress
from apps.competition.services.polling import PollPlanner
from apps.competition.services.publishing import (
    MatchBounds,
    pending_matches,
    publish_catalogue,
)
from apps.competition.services.schedule_notifications import ScheduleChangeDispatcher
from apps.competition.services.sync import (
    EnrichmentWork,
    LiveWork,
    backfill_spacing,
    match_forms_due,
)
from apps.competition.services.traffic import TrafficGate
from apps.schedule.models import Season


LEASE_SECONDS = 120
URGENT_RECHECK_SECONDS = 10
LIVE, HISTORY, ENRICHMENT = "live", "history", "enrichment"


@dataclass(frozen=True)
class TurnOptions:
    """Bound one provider turn and set the live/history share."""

    schedule_changes: ScheduleChangeDispatcher | None
    # Seconds after which no further request starts, then publication follows.
    request_seconds: float = 150
    publish_seconds: float = 200
    live_budget: int | None = None
    history_budget: int = 300
    # Reviewed recovery drains cannot spend requests on other checkpoints.
    history_resource_ids: frozenset[int] | None = None
    # History's share of requests that are not time-critical live work.
    history_share: float = 0.5
    # Opt-in enrichment lane; 0 keeps it closed. Its weight is relative to live
    # (1 - history_share) and history (history_share).
    enrichment_budget: int = 0
    enrichment_share: float = 0.25
    # False when the publication pool publishes in parallel (manager mode).
    publish: bool = True
    # Checked between requests: a stopping process ends the turn cleanly.
    stop: Callable[[], bool] | None = None


@dataclass
class TurnState:
    """Requests per source and which sources can still run this turn."""

    requests: dict[str, int] = field(
        default_factory=lambda: {LIVE: 0, HISTORY: 0, ENRICHMENT: 0}
    )
    open: dict[str, bool] = field(
        default_factory=lambda: {LIVE: True, HISTORY: True, ENRICHMENT: False}
    )
    cooldown: int = 0


def choose(state: TurnState, share: float, enrichment_share: float = 0.0) -> str | None:
    """Pick the open source whose served share is furthest behind its weight.

    Weights are live ``1 - share``, history ``share`` and enrichment
    ``enrichment_share``; ties go to history, then live. A source with no weight
    only runs when no weighted source is open.

    Returns:
        ``"live"``, ``"history"``, ``"enrichment"`` or None when no source has
        work left.

    """
    weights = {LIVE: 1 - share, HISTORY: share, ENRICHMENT: enrichment_share}
    sources = [
        source for source in (HISTORY, LIVE, ENRICHMENT) if state.open.get(source)
    ]
    weighted = [source for source in sources if weights[source] > 0]
    if not weighted:
        return sources[0] if sources else None
    # Deficit order: compare served requests divided by weight, cross-multiplied.
    best = weighted[0]
    for source in weighted[1:]:
        if (
            state.requests.get(source, 0) * weights[best]
            < state.requests.get(best, 0) * weights[source]
        ):
            best = source
    return best


class HistoryWork:
    """Run historical checkpoints one at a time through the shared batch logic."""

    def __init__(
        self,
        client: HistoricalClient,
        gate: TrafficGate,
        *,
        resource_ids: frozenset[int] | None = None,
    ) -> None:
        """Open the batch and read the unpublished backlog once."""
        self.batch = HistoryBatch(client, gate)
        self.resource_ids = resource_ids
        self.backlog = pending_matches().count()
        self.batch.summary["unpublished"] = self.backlog

    def available(self) -> bool:
        """Fetch only while publication keeps up with earlier imports."""
        return (
            self.backlog < PUBLISH_BACKLOG
            and next_resource(resource_ids=self.resource_ids) is not None
        )

    def run_next(self) -> tuple[bool, bool]:
        """Process the next checkpoint.

        Returns:
            Whether a checkpoint was processed, and whether history can continue.

        """
        resource = next_resource(resource_ids=self.resource_ids)
        if resource is None:
            return False, False
        return True, self.batch.process(resource)


def active_live_season() -> Season | None:
    """Return the configured live season while it is in progress."""
    if not settings.SPORTLINK_SYNC_SEASON:
        return None
    today = timezone.localdate()
    return Season.objects.filter(
        name=settings.SPORTLINK_SYNC_SEASON,
        start_date__lte=today,
        end_date__gte=today,
    ).first()


def history_pending(*, resource_ids: frozenset[int] | None = None) -> bool:
    """Tell whether any historical checkpoint is ready."""
    resources = HistoricalResource.objects.filter(
        state="pending", next_attempt_at__lte=timezone.now()
    )
    if resource_ids is not None:
        resources = resources.filter(pk__in=resource_ids)
    return resources.exists()


def claim_lease() -> uuid.UUID | None:
    """Claim the shared provider lease, or return None while it is held."""
    now, owner = timezone.now(), uuid.uuid4()
    lease, _ = SyncLease.objects.get_or_create(
        key="sportlink", defaults={"expires_at": now}
    )
    claimed = SyncLease.objects.filter(pk=lease.pk, expires_at__lte=now).update(
        owner=owner, expires_at=now + timedelta(seconds=LEASE_SECONDS)
    )
    return owner if claimed else None


class ProviderTurn:
    """One lease, one client, both sources, one publication pass."""

    def __init__(
        self,
        season: Season | None,
        clients: Callable[[], tuple[CompetitionClient | None, HistoricalClient]],
        options: TurnOptions,
    ) -> None:
        """Bind the active live season (if any), client factory and bounds."""
        self.season = season
        self.clients = clients
        self.options = options
        self.started = time.monotonic()
        self.state = TurnState()
        self.summary: dict[str, int] = {
            "requests": 0,
            "updated": 0,
            "unchanged": 0,
            "failed": 0,
            "reauth_required": 0,
            "http_requests": 0,
            "deferred": 0,
            "matches_checked": 0,
        }
        self.live: LiveWork | None = None
        self.history: HistoryWork | None = None
        self.enrichment: EnrichmentWork | None = None
        # The request window ended with work still open: start the next turn.
        self.more_work = False
        # Live lane: seconds between routine live requests, and the next slot.
        self.live_spacing = 0
        self.live_next = 0.0
        self.live_deferred = False
        self.urgent_checked: tuple[float, bool] | None = None

    def run(self) -> dict[str, object]:
        """Run the turn under the lease and release it with any cooldown.

        Returns:
            Live counters, plus ``history``, per-source request counts and
            whether work remains for an immediate next turn.

        """
        owner = claim_lease()
        if owner is None:
            return {"status": "busy_or_cooldown", "http_requests": 0}
        client = history_client = None
        try:
            bind_run_lease(owner)
            client, history_client = self.clients()
            self.open_sources(client, history_client, owner)
            self.loop()
            self.publish(owner)
        finally:
            try:
                if history_client is not None:
                    history_client.close()
                elif client is not None:
                    client.close()
            finally:
                SyncLease.objects.filter(key="sportlink", owner=owner).update(
                    owner=None,
                    expires_at=timezone.now() + timedelta(seconds=self.state.cooldown),
                )
        self.summary["elapsed_ms"] = round((time.monotonic() - self.started) * 1000)
        progress("finished", self.summary)
        return {
            **self.summary,
            "history": self.history.batch.summary if self.history else {},
            "enrichment": self.enrichment.finish() if self.enrichment else {},
            "turn_requests": dict(self.state.requests),
            "history_requests": self.state.requests[HISTORY],
            "enrichment_requests": self.state.requests[ENRICHMENT],
            "more_work": self.more_work,
        }

    def open_sources(
        self,
        client: CompetitionClient | None,
        history_client: HistoricalClient,
        owner: uuid.UUID,
    ) -> None:
        """Build the live planner, history batch and enrichment lane with gates.

        Raises:
            ValueError: An active live season has no provider client.

        """
        deadline = self.started + self.options.request_seconds
        summary = self.summary
        if self.season is not None:
            if client is None:
                raise ValueError(
                    "A live provider client is required for an active season"
                )
            enqueue(self.season, "clubs")
            progress("planning", summary)
            planner = PollPlanner(self.season, timezone.now())
            # Live keeps its own rhythm in a lane; the shared provider clock only
            # enforces the smallest gap, so history can fill the time between.
            self.live_spacing = backfill_spacing(planner)
            gate = TrafficGate(
                self.options.live_budget,
                owner,
                deadline=deadline,
                spacing=min(self.live_spacing, history_spacing()),
            )
            summary["request_spacing_seconds"] = self.live_spacing
            self.live = LiveWork(planner, client, gate, summary)
        else:
            self.state.open[LIVE] = False
        if history_pending(resource_ids=self.options.history_resource_ids):
            gate = TrafficGate(
                self.options.history_budget,
                owner,
                deadline=deadline,
                spacing=history_spacing(),
            )
            self.history = HistoryWork(
                history_client, gate, resource_ids=self.options.history_resource_ids
            )
            self.history.batch.publish_deadline = (
                self.started + self.options.publish_seconds
            )
        self.state.open[HISTORY] = self.history is not None and self.history.available()
        # A reviewed, scoped history drain spends its budget on nothing else.
        if (
            client is not None
            and self.options.history_resource_ids is None
            and enrichment_waiting(self.season, budget=self.options.enrichment_budget)
        ):
            self.enrichment = EnrichmentWork(
                CatalogMetadataPlanner(self.season, timezone.now()),
                client,
                EnrichmentGate(
                    self.options.enrichment_budget,
                    owner,
                    deadline=deadline,
                    spacing=history_spacing(),
                ),
                summary,
            )
        self.state.open[ENRICHMENT] = (
            self.enrichment is not None and self.enrichment.available()
        )

    def next_source(self) -> str | None:
        """Time-critical live work first, otherwise the share among ready lanes.

        Routine live requests keep their own spacing; until the live lane is
        ready again, history and enrichment use the time. With only live work
        left, wait for its lane (or close it when the next slot falls after the
        window).
        """
        live_open = self.state.open[LIVE]
        background_open = self.state.open[HISTORY] or self.state.open[ENRICHMENT]
        live_ready = live_open and time.monotonic() >= self.live_next
        if live_open and not live_ready and background_open:
            return LIVE if self.urgent() else self.choose_background()
        if live_open and not live_ready:
            if self.urgent():
                return LIVE
            if self.live_next >= self.started + self.options.request_seconds:
                # Live work remains for the next turn.
                self.live_deferred = True
                self.state.open[LIVE] = False
                return None
            time.sleep(max(0.0, self.live_next - time.monotonic()))
            return LIVE
        source = choose(
            self.state, self.options.history_share, self.options.enrichment_share
        )
        if source in {HISTORY, ENRICHMENT} and live_open and self.urgent():
            return LIVE
        return source

    def choose_background(self) -> str | None:
        """Share the time between paced live requests among background lanes."""
        state = TurnState(
            requests=self.state.requests, open={**self.state.open, LIVE: False}
        )
        return choose(state, self.options.history_share, self.options.enrichment_share)

    def urgent(self) -> bool:
        """Tell whether time-critical live work waits, rechecked at most every 10 s.

        The check walks the season's whole planner (about 0.2 s on production);
        urgent work appears on a scale of minutes. A live request clears the cache.
        """
        if self.live is None:
            return False
        now = time.monotonic()
        if self.urgent_checked is None or now - self.urgent_checked[0] >= (
            URGENT_RECHECK_SECONDS
        ):
            self.urgent_checked = (now, self.live.urgent())
        return self.urgent_checked[1]

    def loop(self) -> None:
        """Pick and run one request at a time until work, budget or time runs out."""
        deadline = self.started + self.options.request_seconds
        while time.monotonic() < deadline:
            if self.options.stop is not None and self.options.stop():
                break
            if match_forms_due():
                # Finish the current request, then let the form worker in.
                self.summary["deferred"] = 1
                break
            source = self.next_source()
            if source is None:
                break
            if source == LIVE:
                if not self.run_live():
                    break
            elif source == ENRICHMENT:
                if not self.run_enrichment():
                    break
            elif not self.run_history():
                break
        # The window usually ends inside a request (a deadline error); any source
        # still open chains the next turn. A cooldown or due form makes that turn
        # return at once.
        self.more_work = self.live_deferred or any(self.state.open.values())

    def run_live(self) -> bool:
        """Run one live feed.

        Returns:
            Whether the turn can continue.

        """
        assert self.live is not None
        ran, cooldown = self.live.run_next()
        self.live_next = time.monotonic() + self.live_spacing
        self.urgent_checked = None
        if not ran or self.live.budget_spent():
            self.state.open[LIVE] = False
        if ran:
            self.state.requests[LIVE] += 1
        if cooldown and not self.live.budget_spent():
            # 429, re-authentication, quota or deadline: the account waits.
            self.state.cooldown = max(self.state.cooldown, cooldown)
            return False
        return True

    def run_history(self) -> bool:
        """Run one historical checkpoint.

        Returns:
            Whether the turn can continue.

        """
        assert self.history is not None
        processed, more = self.history.run_next()
        self.state.requests[HISTORY] += int(processed)
        if more:
            return True
        self.state.open[HISTORY] = False
        batch = self.history.batch
        if batch.stop in {"cooldown", "auth"}:
            # The same app account serves live work: stop the whole turn.
            self.state.cooldown = max(
                self.state.cooldown, batch.cooldown or LEASE_SECONDS // 2
            )
            return False
        return True

    def run_enrichment(self) -> bool:
        """Run one enrichment identity.

        Returns:
            Whether the turn can continue.

        """
        assert self.enrichment is not None
        before_requests = self.enrichment.summary["requests"]
        ran, cooldown = self.enrichment.run_next()
        self.state.requests[ENRICHMENT] += (
            self.enrichment.summary["requests"] - before_requests
        )
        if not ran or not self.enrichment.available():
            self.state.open[ENRICHMENT] = False
        if cooldown:
            # 429, re-authentication, quota or deadline: the account waits.
            self.state.cooldown = max(self.state.cooldown, cooldown)
            return False
        return True

    def publish(self, owner: uuid.UUID) -> None:
        """Publish this season's fixtures, then one bounded chunk of history."""
        summary = self.summary
        if self.live is not None:
            self.live.finish()
            if (
                self.options.publish
                and summary["updated"]
                and self.options.schedule_changes is not None
            ):
                progress("publishing", summary)
                assert self.season is not None
                publication = publish_catalogue(
                    schedule_changes=self.options.schedule_changes,
                    lease_owner=owner,
                    bounds=MatchBounds(seasons={self.season.pk}),
                )
                summary["publication_blocked"] = len(publication["blocked"])
        if self.history is not None:
            batch = self.history.batch
            batch.publish(
                # Without publication only touched poule coverage is reconciled.
                publish_with=(
                    self.options.schedule_changes if self.options.publish else None
                ),
                owner=owner,
                backlog=self.history.backlog,
            )
            batch.summary["http_requests"] = batch.gate.requests
