"""Shared enrichment lane: player photos and club metadata outside the live season.

Photo and club checkpoints are stored per season but describe one person or
club. The live planner reads only the active season, so rows queued in other
seasons (historical lineups, earlier live seasons, historical club logos) had
no consumer. This lane reads them inside the provider turn, with the turn's
lease, request gate and checkpoint code, one global identity at a time:

* ``player_photo``: one request per person and current reference, only while
  the person passes the photo eligibility rules. Retry state is shared: while
  any of the person's rows backs off or is exhausted, no sibling runs.
* club kinds: one request per (kind, club); siblings copy the checkpoint.

The active live season keeps owning its own rows. The lane is opt-in, capped per
turn and optionally per day, and never counts as time-critical work.
"""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta
from typing import Any
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.competition.application.ports import RequestBudgetError
from apps.competition.domain.rosters import ROSTER_FRESHNESS
from apps.competition.models import SyncResource, TrafficState
from apps.competition.services.player_photos import (
    PHOTO_PRIVACY,
    PREFIX,
    eligible_photo_rows,
    photo_candidates,
    photo_name,
    settle_photo_siblings,
)
from apps.competition.services.polling import PollJob
from apps.competition.services.resources import ENDPOINTS, MAX_FEED_FAILURES
from apps.competition.services.traffic import TrafficGate
from apps.player.models import Player
from apps.schedule.models import Season


PHOTO = "player_photo"
CLUB_KINDS = ("club_details", "club_contact", "club_sports", "club_logo")
ENRICHMENT_KINDS = (PHOTO, *CLUB_KINDS)
# Lane-only request counters in the provider traffic table (no extra model).
QUOTA_KEY = "sportlink:enrichment"
BATCH_SIZE = 50
PREVIEW_CHUNK = 2000


class EnrichmentQuotaError(RequestBudgetError):
    """The lane's own daily cap is spent; other provider work continues."""


def enrichment_kinds() -> tuple[str, ...]:
    """Return the supported kinds the deployment enabled for this lane."""
    enabled = set(settings.SPORTLINK_ENRICHMENT_KINDS)
    return tuple(
        kind for kind in ENRICHMENT_KINDS if kind in ENDPOINTS and kind in enabled
    )


def _rows(kind: str, live_season: Season | None) -> QuerySet[SyncResource]:
    """Rows of one kind outside the live season, whose own planner owns them."""
    rows = SyncResource.objects.filter(kind=kind)
    return rows.exclude(season=live_season) if live_season is not None else rows


def held_identities(
    kind: str, live_season: Season | None, now: datetime
) -> QuerySet[SyncResource, dict[str, Any]]:
    """Identities whose retry state or live ownership holds back every sibling.

    A row backing off or at its failure ceiling stops the identity in every
    season, so another season cannot multiply requests or bypass the ceiling.
    """
    held = Q(failures__gte=MAX_FEED_FAILURES) | Q(failures__gt=0, next_sync_at__gt=now)
    if live_season is not None:
        # The live lane refreshes its club rows; it fetches its pending photos.
        owned = Q(season=live_season)
        if kind == PHOTO:
            owned &= Q(fetched_at__isnull=True)
        held |= owned
    return SyncResource.objects.filter(held, kind=kind).values("source_id")


def due_rows(
    kind: str, live_season: Season | None, now: datetime
) -> QuerySet[SyncResource]:
    """Due rows whose identity this lane may request now."""
    rows = _rows(kind, live_season).filter(
        failures__lt=MAX_FEED_FAILURES, next_sync_at__lte=now
    )
    if kind == PHOTO:
        rows = eligible_photo_rows(rows.filter(fetched_at__isnull=True))
    return rows.exclude(source_id__in=held_identities(kind, live_season, now))


def _preference(row: SyncResource) -> tuple[int, date, int]:
    """Continue an existing failure streak first, then the newest season."""
    return (row.failures, row.season.end_date, -row.pk)


class CatalogMetadataPlanner:
    """Plan one enrichment request at a time from bounded keyset batches.

    Kinds take turns so a large photo backlog cannot starve club metadata.
    At most 50 identities enter one provider turn. A person whose current image
    is already saved is settled in its own scheduler step without a request;
    ``settle=False`` only counts such people.
    """

    def __init__(
        self,
        live_season: Season | None,
        now: datetime,
        *,
        kinds: tuple[str, ...] | None = None,
        batch_size: int = BATCH_SIZE,
        settle: bool = True,
    ) -> None:
        """Bind the season the live lane owns and the planning time."""
        self.live_season = live_season
        self.now = now
        self.kinds = enrichment_kinds() if kinds is None else kinds
        self.remaining = min(batch_size, BATCH_SIZE)
        self.batch_size = max(1, self.remaining // max(1, len(self.kinds)))
        self.settle = settle
        self.cursors = dict.fromkeys(self.kinds, "")
        self.queues: dict[str, deque[PollJob]] = {kind: deque() for kind in self.kinds}
        self.drained: set[str] = set()
        self.rotation = 0
        self.summary = {
            "settled_locally": 0,
            "rows_settled": 0,
            "skipped": 0,
            "identities_loaded": 0,
            "batch_limit_reached": 0,
        }

    def candidate_jobs(self) -> list[PollJob]:
        """Expose the loaded, not yet requested jobs."""
        return [job for queue in self.queues.values() for job in queue]

    def available(self) -> bool:
        """Read one bounded batch if needed, without settling or writing rows."""
        return any(self._fill(kind) for kind in self.kinds)

    def next_job(self) -> PollJob | None:
        """Take the next kind's job whose row is still due."""
        for _ in self.kinds:
            kind = self.kinds[self.rotation % len(self.kinds)]
            self.rotation += 1
            while self._fill(kind):
                job = self.queues[kind].popleft()
                # Another lane may have completed the identity since loading.
                fresh = (
                    due_rows(kind, self.live_season, timezone.now())
                    .filter(pk=job.resource.pk)
                    .select_related("season")
                    .first()
                )
                if fresh is not None:
                    return PollJob(fresh, set(), 3)
                self.summary["skipped"] += 1
        return None

    def _fill(self, kind: str) -> bool:
        queue = self.queues[kind]
        if not queue and kind not in self.drained and self.remaining > 0:
            self._load(kind)
        return bool(queue)

    def _load(self, kind: str) -> None:
        """Load the next batch of identities after this kind's cursor."""
        rows = due_rows(kind, self.live_season, self.now).filter(
            source_id__gt=self.cursors[kind]
        )
        limit = min(self.batch_size, self.remaining)
        identities = list(
            rows
            .order_by("source_id")
            .values_list("source_id", flat=True)
            .distinct()[:limit]
        )
        self.remaining -= len(identities)
        self.summary["identities_loaded"] += len(identities)
        self.summary["batch_limit_reached"] = int(self.remaining == 0)
        if len(identities) < limit:
            self.drained.add(kind)
        if not identities:
            return
        self.cursors[kind] = identities[-1]
        best: dict[str, SyncResource] = {}
        for row in rows.filter(source_id__in=identities).select_related("season"):
            current = best.get(row.source_id)
            if current is None or _preference(row) > _preference(current):
                best[row.source_id] = row
        # A missing identity was completed by another lane between the two reads.
        self.queues[kind].extend(
            PollJob(best[source_id], set(), 3)
            for source_id in identities
            if source_id in best
        )

    def settle_cached(self, job: PollJob) -> bool:
        """Settle one saved photo, yielding to the scheduler before the next one."""
        if job.resource.kind != PHOTO:
            return False
        if self.settle:
            count = settle_photo_siblings(job.resource.source_id, self.now)
            if not count:
                return False
            self.summary["rows_settled"] += count
        else:
            player = (
                photo_candidates()
                .filter(pk=job.resource.source_id)
                .only("pk", "knkv_photo", "profile_picture")
                .first()
            )
            if player is None or player.profile_picture.name != photo_name(player):
                return False
        self.summary["settled_locally"] += 1
        return True


def settle_siblings(resource: SyncResource, now: datetime) -> int:
    """Complete the identity's rows in other seasons after a successful checkpoint.

    Returns:
        The number of sibling rows updated.

    """
    if resource.kind == PHOTO:
        return settle_photo_siblings(resource.source_id, now)
    if resource.kind not in CLUB_KINDS:
        return 0
    return (
        SyncResource.objects
        .filter(kind=resource.kind, source_id=resource.source_id)
        .exclude(pk=resource.pk)
        .update(
            fetched_at=resource.fetched_at,
            next_sync_at=resource.next_sync_at,
            etag=resource.etag,
            failures=0,
            last_error="",
        )
    )


def quota_retry_at(now: datetime | None = None) -> datetime | None:
    """Return when the lane's daily cap reopens, or None while requests remain."""
    limit = settings.SPORTLINK_ENRICHMENT_DAILY_LIMIT
    if not limit:
        return None
    now = now or timezone.now()
    state = TrafficState.objects.filter(key=QUOTA_KEY).first()
    if state is None or now >= state.day_start + timedelta(days=1):
        return None
    return state.day_start + timedelta(days=1) if state.day_requests >= limit else None


def count_request(now: datetime | None = None) -> None:
    """Count one reserved lane request in its durable rolling counters."""
    now = now or timezone.now()
    with transaction.atomic():
        state, _ = TrafficState.objects.select_for_update().get_or_create(
            key=QUOTA_KEY,
            defaults={"hour_start": now, "day_start": now, "next_request_at": now},
        )
        if now >= state.hour_start + timedelta(hours=1):
            state.hour_start, state.hour_requests = now, 0
        if now >= state.day_start + timedelta(days=1):
            state.day_start, state.day_requests = now, 0
        state.hour_requests += 1
        state.day_requests += 1
        state.save(
            update_fields=("hour_start", "hour_requests", "day_start", "day_requests")
        )


class EnrichmentGate(TrafficGate):
    """The provider gate plus the lane's own durable daily cap."""

    def before_request(self) -> None:
        """Check the lane cap, reserve through the shared gate, then count.

        Raises:
            EnrichmentQuotaError: The lane's daily cap is spent.

        """
        retry_at = quota_retry_at()
        if retry_at is not None:
            raise EnrichmentQuotaError(retry_at)
        super().before_request()
        count_request()


def enrichment_waiting(live_season: Season | None, *, budget: int) -> bool:
    """Tell whether the enabled lane has a request it may make now."""
    if budget <= 0 or quota_retry_at() is not None:
        return False
    now = timezone.now()
    return any(due_rows(kind, live_season, now).exists() for kind in enrichment_kinds())


def preview_photos(live_season: Season | None, now: datetime) -> dict[str, object]:
    """Classify every queued person without HTTP requests or writes.

    Each person counts once, in the first matching outcome: held back by the
    live lane or a sibling's retry state, not yet due, no longer a player,
    hidden by ``Player.objects``, privacy, missing reference, native upload,
    already saved (local settlement), or eligible for one request.
    """
    queued = _rows(PHOTO, live_season).filter(fetched_at__isnull=True)
    people: dict[str, dict[str, Any]] = {
        row["source_id"]: row
        for row in queued
        .values("source_id")
        .annotate(
            rows=Count("pk"),
            due=Count(
                "pk",
                filter=Q(failures__lt=MAX_FEED_FAILURES, next_sync_at__lte=now),
            ),
        )
        .order_by()
    }
    held = _held_outcomes(live_season, now)
    outcomes = dict.fromkeys(
        (
            "live_owned",
            "exhausted",
            "backing_off",
            "not_due",
            "missing_player",
            "archived",
            "hidden_stale_or_withdrawn",
            "privacy_not_permitted",
            "no_reference",
            "native_upload",
            "already_saved",
            "eligible",
        ),
        0,
    )
    freshness = dict.fromkeys(
        ("no_expiry", "under_1_day", "1_to_3_days", "3_to_8_days"), 0
    )
    settle_rows = 0
    remaining = []
    for source_id, counts in people.items():
        outcome = next(
            (name for name, ids in held.items() if source_id in ids),
            None if counts["due"] else "not_due",
        )
        if outcome is None:
            remaining.append(source_id)
        else:
            outcomes[outcome] += 1
    for start in range(0, len(remaining), PREVIEW_CHUNK):
        keys = {}
        for source_id in remaining[start : start + PREVIEW_CHUNK]:
            try:
                keys[uuid.UUID(source_id)] = source_id
            except ValueError:
                outcomes["missing_player"] += 1
        visible = set(Player.objects.filter(pk__in=keys).values_list("pk", flat=True))
        found = set()
        for player in Player.all_objects.filter(pk__in=keys).only(
            "pk",
            "user_id",
            "archived_at",
            "knkv_privacy",
            "knkv_observed_at",
            "knkv_photo",
            "profile_picture",
        ):
            found.add(player.pk)
            outcome = _player_outcome(player, visible=player.pk in visible)
            outcomes[outcome] += 1
            if outcome == "already_saved":
                settle_rows += people[keys[player.pk]]["rows"]
            elif outcome == "eligible":
                freshness[_freshness_bucket(player, now)] += 1
        outcomes["missing_player"] += len(set(keys) - found)
    return {
        "dry_run": True,
        "http_requests": 0,
        "live_season": live_season.name if live_season is not None else None,
        "queued_rows": sum(row["rows"] for row in people.values()),
        "queued_people": len(people),
        "outcomes": outcomes,
        "local_settlement_rows": settle_rows,
        "eligible_freshness": freshness,
        "request_estimate": outcomes["eligible"],
        "lane_enabled": settings.SPORTLINK_ENRICHMENT_MAX_REQUESTS > 0
        and PHOTO in enrichment_kinds(),
        "lane_requests_per_turn": settings.SPORTLINK_ENRICHMENT_MAX_REQUESTS,
        "lane_daily_limit": settings.SPORTLINK_ENRICHMENT_DAILY_LIMIT,
        "club_identities_due": {
            kind: due_rows(kind, live_season, now)
            .values("source_id")
            .distinct()
            .count()
            for kind in CLUB_KINDS
            if kind in ENDPOINTS
        },
        "note": (
            "One request per eligible person before token renewal and retries. "
            "Eligibility is rechecked before and after each download, so people "
            "who turn stale, private or upload their own photo are skipped."
        ),
    }


def _held_outcomes(live_season: Season | None, now: datetime) -> dict[str, set[str]]:
    """People held back by the live lane or a sibling's retry state, in order."""
    rows = SyncResource.objects.filter(kind=PHOTO)
    live_owned = (
        rows.filter(season=live_season, fetched_at__isnull=True)
        if live_season is not None
        else rows.none()
    )
    return {
        "live_owned": set(live_owned.values_list("source_id", flat=True)),
        "exhausted": set(
            rows.filter(failures__gte=MAX_FEED_FAILURES).values_list(
                "source_id", flat=True
            )
        ),
        "backing_off": set(
            rows.filter(failures__gt=0, next_sync_at__gt=now).values_list(
                "source_id", flat=True
            )
        ),
    }


def _player_outcome(player: Player, *, visible: bool) -> str:
    """Apply the photo eligibility rules in the order the lane checks them."""
    current = player.profile_picture.name or ""
    if player.archived_at is not None:
        return "archived"
    if not visible:
        return "hidden_stale_or_withdrawn"
    if player.knkv_privacy not in PHOTO_PRIVACY:
        return "privacy_not_permitted"
    if not player.knkv_photo:
        return "no_reference"
    if current and not current.startswith(PREFIX):
        return "native_upload"
    return "already_saved" if current == photo_name(player) else "eligible"


def _freshness_bucket(player: Player, now: datetime) -> str:
    """Time left before an imported identity leaves Player.objects."""
    if player.user_id is not None or player.knkv_observed_at is None:
        # Accounts (and players without a provider identity) stay visible.
        return "no_expiry"
    left = player.knkv_observed_at + ROSTER_FRESHNESS - now
    if left < timedelta(days=1):
        return "under_1_day"
    return "1_to_3_days" if left < timedelta(days=3) else "3_to_8_days"
