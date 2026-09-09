"""Schedule result checks around match lifecycle, not around page views."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Q

from apps.competition.models import Match, Pool, SyncResource
from apps.competition.services.resources import MAX_FEED_FAILURES
from apps.schedule.models import Season


EXPECTED_DURATION = timedelta(minutes=75)
RESULT_INTERVAL = timedelta(minutes=5)
RECENT_RESULT_WINDOW = timedelta(hours=3)
PENDING_HOURLY_WINDOW = timedelta(hours=48)
CORRECTION_DAILY_WINDOW = timedelta(days=7)
CORRECTION_WEEKLY_WINDOW = timedelta(days=28)
DISCOVERY_SLOT_INTERVAL = 5
MINIMUM_FEED_INTERVAL = RESULT_INTERVAL
UPCOMING_SCHEDULE_WINDOW = timedelta(days=2)
UPCOMING_SCHEDULE_INTERVAL = timedelta(hours=1)


def next_result_check(row: dict[str, Any], now: datetime) -> datetime:
    """Return the next useful check; kickoff plus 75 minutes is a heuristic."""
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
        self.season = season
        self.resources = {
            (resource.kind, resource.source_id): resource
            for resource in SyncResource.objects.filter(
                season=season, failures__lt=MAX_FEED_FAILURES
            ).select_related("season")
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

    def _add_program_jobs(self, row: dict[str, Any], jobs: dict[int, PollJob]) -> None:
        """Refresh shared schedules hourly around upcoming/unresolved fixtures."""
        if (
            row["status"] not in {"FINAL", "WITHDRAWN", "CANCELLED"}
            and row["home_score"] is None
            and row["away_score"] is None
            and self.now - timedelta(days=1)
            <= row["starts_at"]
            <= self.now + UPCOMING_SCHEDULE_WINDOW
        ):
            for club in {
                row["home_team__club__external_id"],
                row["away_team__club__external_id"],
            }:
                program = self.resources.get(("club_program", club))
                if (
                    program
                    and self._available(program)
                    and (
                        program.fetched_at is None
                        or program.fetched_at + UPCOMING_SCHEDULE_INTERVAL <= self.now
                    )
                ):
                    job = jobs.setdefault(program.pk, PollJob(program, set(), 1))
                    job.priority = min(job.priority, 1)

    def candidate_jobs(self) -> list[PollJob]:
        """Collect eligible feeds in one pass without selecting or marking work."""
        jobs = {
            resource.pk: PollJob(
                resource, set(), 2 if resource.fetched_at is None else 3
            )
            for resource in self.resources.values()
            if self._available(resource) and resource.next_sync_at <= self.now
        }
        for row in self.rows:
            self._add_program_jobs(row, jobs)
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
        return list(jobs.values())

    def next_job(self) -> PollJob | None:
        """Prefer shared checks while reserving periodic slots for discovery."""
        jobs = self.candidate_jobs()
        if not jobs:
            return None
        # Reserve one in five selections for first-time collection discovery.
        bootstrap = [job for job in jobs if job.resource.fetched_at is None]
        candidates = (
            bootstrap
            if len(self.attempted) % DISCOVERY_SLOT_INTERVAL
            == DISCOVERY_SLOT_INTERVAL - 1
            and bootstrap
            else jobs
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

    def result_metrics(self) -> dict[str, int]:
        """Measure observed coverage and first final-score delay for this batch."""
        previous_finals = {
            row["id"]
            for row in self.rows
            if row["status"] == "FINAL"
            and row["home_score"] is not None
            and row["away_score"] is not None
        }
        observed = list(
            Match.objects.filter(
                season=self.season,
                result_observed_at__gte=self.now,
            ).values(
                "id",
                "status",
                "home_score",
                "away_score",
                "starts_at",
                "result_observed_at",
            )
        )
        delays = [
            max(
                0,
                int(
                    (
                        row["result_observed_at"] - row["starts_at"] - EXPECTED_DURATION
                    ).total_seconds()
                ),
            )
            for row in observed
            if row["id"] not in previous_finals
            and row["status"] == "FINAL"
            and row["home_score"] is not None
            and row["away_score"] is not None
        ]
        return {
            "results_observed": len(observed),
            "new_final_results": len(delays),
            "result_delay_seconds_total": sum(delays),
            "result_delay_seconds_max": max(delays, default=0),
        }

    def completed(self, job: PollJob, *, checked: bool) -> None:
        """Avoid opponent-feed duplicates within the batch after a successful check."""
        if job.resource.kind == "pool_results" and not checked:
            for pool in self.pools.values():
                if pool["external_id"] == job.resource.source_id:
                    pool["results_filtered"] = True
        # Responses can include fixtures outside the job's planned scope. Reuse
        # only actual observations committed since this batch snapshot; a filtered
        # or failed response must never imply coverage of absent matches.
        if job.resource.kind in {"club_results", "pool_results"}:
            self.checked.update(
                Match.objects.filter(
                    season=self.season, result_observed_at__gte=self.now
                ).values_list("pk", flat=True)
            )
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
