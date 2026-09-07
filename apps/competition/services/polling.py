"""Schedule result checks around match lifecycle, not around page views."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Q

from apps.competition.models import Match, Pool, SyncResource
from apps.schedule.models import Season


EXPECTED_DURATION = timedelta(minutes=90)
RESULT_INTERVAL = timedelta(minutes=15)
RECENT_RESULT_WINDOW = timedelta(hours=3)
PENDING_HOURLY_WINDOW = timedelta(hours=48)
CORRECTION_DAILY_WINDOW = timedelta(days=7)
CORRECTION_WEEKLY_WINDOW = timedelta(days=28)
DISCOVERY_SLOT_INTERVAL = 5
MINIMUM_FEED_INTERVAL = timedelta(minutes=15)


def next_result_check(row: dict[str, Any], now: datetime) -> datetime:
    """Return the next useful check; kickoff plus 90 minutes is a heuristic."""
    finish = row["starts_at"] + EXPECTED_DURATION
    checked = row["results_checked_at"] or row["result_observed_at"]
    if row["status"] in {"CANCELLED", "WITHDRAWN", "POSTPONED"}:
        return (checked or finish) + timedelta(days=7)
    final = (
        row["status"] == "FINAL"
        and row["home_score"] is not None
        and row["away_score"] is not None
    )
    if final:
        age = now - finish
        days = (
            1
            if age <= CORRECTION_DAILY_WINDOW
            else 7
            if age <= CORRECTION_WEEKLY_WINDOW
            else 30
        )
        return (checked or finish) + timedelta(days=days)
    if checked is None or now < finish:
        return finish
    age = now - finish
    interval = RESULT_INTERVAL
    if age > RECENT_RESULT_WINDOW:
        interval = timedelta(hours=1)
    if age > PENDING_HOURLY_WINDOW:
        interval = timedelta(days=1)
    if age > timedelta(days=30):
        interval = timedelta(days=7)
    return max(finish, checked + interval)


@dataclass
class PollJob:
    """One shared feed and the due matches it can check together."""

    resource: SyncResource
    matches: set[int]
    priority: int


class PollPlanner:
    """Plan a batch from one local snapshot and coalesce overlapping result feeds.

    Complete, healthy poule feeds own their matches. Club feeds provide fallback
    for unpooled/filtered/unavailable poules, choosing the club covering most due
    matches first. New resources discovered during a batch enter the next run.
    """

    def __init__(self, season: Season, now: datetime) -> None:
        """Load one season's compact scheduling snapshot without provider traffic."""
        self.now = now
        self.resources = {
            (resource.kind, resource.source_id): resource
            for resource in SyncResource.objects.filter(season=season).select_related(
                "season"
            )
        }
        self.rows = list(
            Match.objects.filter(season=season).values(
                "id",
                "pool_id",
                "home_team__club__external_id",
                "away_team__club__external_id",
                "starts_at",
                "status",
                "home_score",
                "away_score",
                "results_checked_at",
                "result_observed_at",
            )
        )
        pools = Pool.objects.filter(season=season).values(
            "id", "external_id", "results_filtered"
        )
        self.pools = {pool["id"]: pool for pool in pools}
        self.club_matches: dict[str, set[int]] = {}
        for row in self.rows:
            for club in {
                row["home_team__club__external_id"],
                row["away_team__club__external_id"],
            }:
                self.club_matches.setdefault(club, set()).add(row["id"])
        self.attempted: set[int] = set()
        self.checked: set[int] = set()

    def _available(self, resource: SyncResource) -> bool:
        """Respect feed pacing and retry deadlines, even for urgent matches."""
        if resource.pk in self.attempted:
            return False
        if resource.failures and resource.next_sync_at > self.now:
            return False
        return (
            resource.kind not in {"club_results", "pool_results"}
            or resource.fetched_at is None
            or resource.fetched_at + MINIMUM_FEED_INTERVAL <= self.now
        )

    def _owners(self, row: dict[str, Any]) -> list[SyncResource]:
        """Use one complete poule feed where possible, otherwise a club fallback."""
        pool = self.pools.get(row["pool_id"])
        if pool and not pool["results_filtered"]:
            resource = self.resources.get(("pool_results", pool["external_id"]))
            if resource and resource.fetched_at and not resource.failures:
                return [resource]
        return [
            resource
            for club in {
                row["home_team__club__external_id"],
                row["away_team__club__external_id"],
            }
            if (resource := self.resources.get(("club_results", club))) is not None
        ]

    def next_job(self) -> PollJob | None:
        """Prefer shared checks while reserving periodic slots for discovery."""
        jobs = {
            resource.pk: PollJob(
                resource, set(), 2 if resource.fetched_at is None else 3
            )
            for resource in self.resources.values()
            if self._available(resource) and resource.next_sync_at <= self.now
        }
        for row in self.rows:
            if row["id"] in self.checked or next_result_check(row, self.now) > self.now:
                continue
            for resource in self._owners(row):
                if not self._available(resource):
                    continue
                job = jobs.setdefault(resource.pk, PollJob(resource, set(), 1))
                job.matches.add(row["id"])
                recent_pending = (
                    row["status"] != "FINAL"
                    and self.now - row["starts_at"] <= PENDING_HOURLY_WINDOW
                )
                job.priority = min(job.priority, 0 if recent_pending else 1)
        if not jobs:
            return None
        # Reserve one in five selections for first-time collection discovery.
        bootstrap = [job for job in jobs.values() if job.resource.fetched_at is None]
        candidates = (
            bootstrap
            if len(self.attempted) % DISCOVERY_SLOT_INTERVAL
            == DISCOVERY_SLOT_INTERVAL - 1
            and bootstrap
            else list(jobs.values())
        )
        job = min(
            candidates,
            key=lambda item: (
                item.priority,
                -len(item.matches),
                item.resource.next_sync_at,
                item.resource.pk,
            ),
        )
        self.attempted.add(job.resource.pk)
        return job

    def completed(self, job: PollJob, *, checked: bool) -> None:
        """Avoid opponent-feed duplicates within the batch after a successful check."""
        if job.resource.kind == "pool_results" and not checked:
            for pool in self.pools.values():
                if pool["external_id"] == job.resource.source_id:
                    pool["results_filtered"] = True
        if checked:
            self.checked.update(job.matches)
            for resource in self.resources.values():
                if resource.kind != "club_results" or resource.fetched_at is None:
                    continue
                related = self.club_matches.get(resource.source_id, set())
                if related and related <= self.checked:
                    self.attempted.add(resource.pk)


def mark_checked(job: PollJob, now: datetime) -> bool:
    """Record a successful scope check separately from observing an actual result."""
    if (
        job.resource.kind == "pool_results"
        and Pool.objects.filter(
            season=job.resource.season,
            external_id=job.resource.source_id,
            results_filtered=True,
        ).exists()
    ):
        return False
    Match.objects.filter(season=job.resource.season, pk__in=job.matches).filter(
        Q(results_checked_at__isnull=True) | Q(results_checked_at__lt=now)
    ).update(results_checked_at=now)
    return True
