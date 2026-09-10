"""Schedule result checks around match lifecycle, not around page views."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.competition.domain.timing import expected_finish
from apps.competition.models import Match, Pool, SyncResource
from apps.competition.services.match_details import DETAIL_FIELDS, source_matches
from apps.competition.services.resources import MAX_FEED_FAILURES
from apps.schedule.models import Season


RESULT_INTERVAL = timedelta(minutes=3)
RECENT_RESULT_WINDOW = timedelta(hours=3)
PENDING_HOURLY_WINDOW = timedelta(hours=48)
CORRECTION_DAILY_WINDOW = timedelta(days=7)
CORRECTION_WEEKLY_WINDOW = timedelta(days=28)
DISCOVERY_SLOT_INTERVAL = 5
MINIMUM_FEED_INTERVAL = RESULT_INTERVAL
UPCOMING_SCHEDULE_WINDOW = timedelta(days=2)
UPCOMING_SCHEDULE_INTERVAL = timedelta(hours=1)
IMMINENT_SCHEDULE_INTERVAL = timedelta(minutes=15)
MINIMUM_REPORTING_SAMPLES = 12


MATCH_FIELDS = (
    "id",
    "pool_id",
    "season__start_date",
    "playing_time_minutes",
    "playing_time_observed_at",
    "facility_observed_at",
    "rules_observed_at",
    "external_id",
    "reporting_delay_seconds",
    "schedule_checked_at",
    "pool__mapping_status",
    "pool__competition_class_id",
    "pool__competition_class__category",
    "pool__competition_class__age_group",
    "pool__competition_class__colour",
    "pool__competition_class__playing_format",
    "pool__competition_class__edition__discipline",
    "home_team__club__external_id",
    "away_team__club__external_id",
    "starts_at",
    "status",
    "home_score",
    "away_score",
    "results_checked_at",
    "result_observed_at",
)


def next_result_check(row: dict[str, Any], now: datetime) -> datetime:
    """Use playing format and bounded learned reporting delay for due times."""
    finish = expected_finish(row) + timedelta(
        seconds=row.get("learned_delay_seconds", 0)
    )
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
    schedule_matches: set[int] = field(default_factory=set)
    covered_matches: set[int] = field(default_factory=set)
    urgent_matches: set[int] = field(default_factory=set)


class MetadataPlanner:
    """Drain a due metadata snapshot without loading the match polling graph."""

    def __init__(self, season: Season, now: datetime) -> None:
        """Filter missing components in SQL and order the queue once per run."""
        missing = Q(pk__in=[])
        for kind, stamp in DETAIL_FIELDS.items():
            missing |= Q(
                kind=kind,
                source_id__in=source_matches(season)
                .filter(**{stamp + "__isnull": True})
                .values("external_id"),
            )
        self.jobs = deque(
            PollJob(resource, set(), 2)
            for resource in SyncResource.objects
            .filter(
                missing,
                season=season,
                failures__lt=MAX_FEED_FAILURES,
                next_sync_at__lte=now,
            )
            .select_related("season")
            .order_by("next_sync_at", "pk")
        )
        self.checked: set[int] = set()
        self.schedule_checked: set[int] = set()

    def candidate_jobs(self) -> list[PollJob]:
        """Expose remaining work for previews and budget reporting."""
        return list(self.jobs)

    def next_job(self) -> PollJob | None:
        """Select the next component in constant time without database reads."""
        return self.jobs.popleft() if self.jobs else None

    def completed(self, job: PollJob, *, checked: bool) -> None:
        """Checkpoints already persist completion; no match graph needs refreshing."""

    def result_metrics(self) -> dict[str, int]:
        """Metadata endpoints do not observe scores or teach reporting delays."""
        return dict.fromkeys(
            (
                "reporting_samples_added",
                "results_observed",
                "new_final_results",
                "result_delay_seconds_total",
                "result_delay_seconds_max",
            ),
            0,
        )


class PollPlanner:
    """Plan a batch from one local snapshot and coalesce overlapping result feeds.

    Compete healthy poule and club feeds by marginal due-match coverage.
    Only observed response membership suppresses overlapping checks; routine
    discovery still audits every feed. New resources enter the next run.
    """

    def __init__(self, season: Season, now: datetime) -> None:
        """Load one season's compact scheduling snapshot without provider traffic."""
        self.now = now
        self.snapshot_at = now
        self.season = season
        self.resources = {
            (resource.kind, resource.source_id): resource
            for resource in SyncResource.objects
            .filter(season=season, failures__lt=MAX_FEED_FAILURES)
            .exclude(kind__in=DETAIL_FIELDS)
            .select_related("season")
        }
        self.metadata = MetadataPlanner(season, now)
        self.metadata_only_until = now
        self.metadata_changed: set[int] = set()
        self.rows = list(Match.objects.filter(season=season).values(*MATCH_FIELDS))
        pools = Pool.objects.filter(season=season).values(
            "id", "external_id", "results_filtered"
        )
        self.pools = {pool["id"]: pool for pool in pools}
        self.attempted: set[int] = set()
        self.checked: set[int] = set()
        self.schedule_checked: set[int] = set()
        self.row_by_id = {row["id"]: dict(row) for row in self.rows}
        self.current_by_source = {row["external_id"]: row for row in self.rows}
        self._apply_class_durations()
        self._learn_reporting_delays()

    def _apply_class_durations(self) -> None:
        """Reuse unambiguous recent details within the same mapped class."""
        values: dict[int, set[int]] = {}
        for row in self.rows:
            context = row["pool__competition_class_id"]
            observed = row["playing_time_observed_at"]
            if (
                context
                and observed
                and observed + timedelta(days=30) > self.now
                and row["playing_time_minutes"]
            ):
                values.setdefault(context, set()).add(row["playing_time_minutes"])
        for row in self.rows:
            durations = values.get(row["pool__competition_class_id"], set())
            row.pop("class_playing_time_minutes", None)
            if len(durations) == 1:
                row["class_playing_time_minutes"] = next(iter(durations))

    def _learn_reporting_delays(self) -> None:
        """Use a lower decile of well-observed finals; never delay by over 15 min."""
        samples: dict[int, list[int]] = {}
        for row in self.rows:
            context = row["pool__competition_class_id"]
            delay = row["reporting_delay_seconds"]
            if context and delay is not None and row["status"] == "FINAL":
                samples.setdefault(context, []).append(delay)
        learned = {
            context: min(900, sorted(values)[len(values) // 10])
            for context, values in samples.items()
            if len(values) >= MINIMUM_REPORTING_SAMPLES
        }
        for row in self.rows:
            if row["pool__competition_class_id"] in learned:
                row["learned_delay_seconds"] = learned[
                    row["pool__competition_class_id"]
                ]

    def _available(self, resource: SyncResource) -> bool:
        """Respect feed pacing and retry deadlines, even for urgent matches."""
        if resource.pk in self.attempted:
            return False
        if resource.kind in DETAIL_FIELDS:
            fixture = self.current_by_source.get(resource.source_id)
            if fixture and fixture[DETAIL_FIELDS[resource.kind]]:
                return False
        if resource.failures and resource.next_sync_at > self.now:
            return False
        return (
            resource.kind not in {"club_results", "pool_results"}
            or resource.fetched_at is None
            or resource.fetched_at + MINIMUM_FEED_INTERVAL <= self.now
        )

    def _owners(self, row: dict[str, Any]) -> list[SyncResource]:
        """Offer all plausible feeds; healthy pools win equal-coverage ties."""
        owners = []
        pool = self.pools.get(row["pool_id"])
        if pool and not pool["results_filtered"]:
            resource = self.resources.get(("pool_results", pool["external_id"]))
            if resource and resource.fetched_at and not resource.failures:
                owners.append(resource)
        return owners + [
            resource
            for club in {
                row["home_team__club__external_id"],
                row["away_team__club__external_id"],
            }
            if (resource := self.resources.get(("club_results", club))) is not None
        ]

    def _add_program_jobs(self, row: dict[str, Any], jobs: dict[int, PollJob]) -> None:
        """Refresh shared schedules hourly around upcoming/unresolved fixtures."""
        interval = (
            IMMINENT_SCHEDULE_INTERVAL
            if abs((row["starts_at"] - self.now).total_seconds()) <= 6 * 3600
            else UPCOMING_SCHEDULE_INTERVAL
        )
        if row["id"] in self.schedule_checked or (
            row["schedule_checked_at"]
            and row["schedule_checked_at"] + interval > self.now
        ):
            return
        if (
            row["status"] not in {"FINAL", "WITHDRAWN"}
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
                        or program.fetched_at + interval <= self.now
                    )
                ):
                    job = jobs.setdefault(program.pk, PollJob(program, set(), 1))
                    job.priority = min(job.priority, 1)
                    job.schedule_matches.add(row["id"])

    def candidate_jobs(self, *, include_metadata: bool = True) -> list[PollJob]:
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
                if recent_pending:
                    job.urgent_matches.add(row["id"])
        return list(jobs.values()) + (
            [job for job in self.metadata.jobs if self._available(job.resource)]
            if include_metadata
            else []
        )

    def next_job(self) -> PollJob | None:
        """Prefer shared checks while reserving periodic slots for discovery."""
        self.now = max(self.now, timezone.now())
        catching_up = bool(self.metadata.jobs)
        if catching_up and self.now < self.metadata_only_until:
            return self._next_metadata()
        if self.metadata_changed:
            self._refresh_matches(self.metadata_changed)
            self.metadata_changed.clear()
            self._apply_class_durations()
        jobs = self.candidate_jobs(include_metadata=False)
        if catching_up:
            # Due results and schedule checks keep priority, including the slots
            # otherwise reserved for routine discovery/audits.
            jobs = [job for job in jobs if job.matches or job.schedule_matches]
            if not jobs:
                self.metadata_only_until = self.now + timedelta(seconds=30)
                return self._next_metadata()
        if not jobs:
            return None
        # Reserve one in five selections for discovery or overdue audits.
        bootstrap = [
            job
            for job in jobs
            if job.resource.fetched_at is None
            or job.resource.next_sync_at + timedelta(hours=6) <= self.now
        ]
        candidates = (
            bootstrap
            if len(self.attempted) % DISCOVERY_SLOT_INTERVAL
            == DISCOVERY_SLOT_INTERVAL - 1
            and bootstrap
            and not catching_up
            else jobs
        )
        job = min(
            candidates,
            key=lambda item: (
                item.priority,
                -len(self._likely_coverage(item, item.urgent_matches)),
                -len(self._likely_coverage(item, item.matches | item.schedule_matches)),
                -len(item.urgent_matches),
                -len(item.matches | item.schedule_matches),
                0 if item.resource.kind == "pool_results" else 1,
                item.resource.next_sync_at,
                item.resource.pk,
            ),
        )
        self.attempted.add(job.resource.pk)
        return job

    def _next_metadata(self) -> PollJob | None:
        """Skip components observed by another feed since the queue was loaded."""
        while (job := self.metadata.next_job()) is not None:
            if self._available(job.resource):
                self.attempted.add(job.resource.pk)
                return job
        self.metadata_only_until = self.now
        return self.next_job()

    def _likely_coverage(self, job: PollJob, candidates: set[int]) -> set[int]:
        """Use recent membership to rank feeds, never to remove due work.

        Empty membership is also used by legacy/unknown checkpoints, so it cannot
        prove an empty scope. Daily audits and unknown feeds remain eligible.
        """
        resource = job.resource
        if (
            resource.match_ids
            and resource.fetched_at
            and resource.fetched_at + timedelta(days=1) > self.now
        ):
            return candidates.intersection(resource.match_ids)
        return candidates

    def result_metrics(self) -> dict[str, int]:
        """Measure observed coverage and first final-score delay for this batch."""
        previous_finals = {
            row["id"]
            for row in self.row_by_id.values()
            if row["status"] == "FINAL"
            and row["home_score"] is not None
            and row["away_score"] is not None
        }
        observed = list(
            Match.objects.filter(
                season=self.season,
                result_observed_at__gte=self.snapshot_at,
            ).values(*MATCH_FIELDS)
        )
        delays = [
            max(
                0,
                int((row["result_observed_at"] - expected_finish(row)).total_seconds()),
            )
            for row in observed
            if row["id"] not in previous_finals
            and row["status"] == "FINAL"
            and row["home_score"] is not None
            and row["away_score"] is not None
        ]
        samples = []
        measured_delays = []
        for result in observed:
            previous = self.row_by_id.get(result["id"])
            if not self._usable_reporting_sample(previous, result):
                continue
            assert previous is not None
            measured_delays.append(
                max(
                    0,
                    int(
                        (
                            result["result_observed_at"] - expected_finish(previous)
                        ).total_seconds()
                    ),
                )
            )
            # Subtract the observation uncertainty: a delayed polling worker
            # must not teach future workers to wait longer.
            delay = max(
                0,
                int(
                    (
                        previous["result_observed_at"] - expected_finish(previous)
                    ).total_seconds()
                ),
            )
            samples.append(Match(pk=result["id"], reporting_delay_seconds=delay))
        if samples:
            Match.objects.bulk_update(samples, ["reporting_delay_seconds"])
        return {
            "measured_final_results": len(measured_delays),
            "measured_delay_seconds_total": sum(measured_delays),
            "measured_delay_seconds_max": max(measured_delays, default=0),
            "unmeasured_final_results": len(delays) - len(measured_delays),
            "reporting_samples_added": len(samples),
            "results_observed": len(observed),
            "new_final_results": len(delays),
            "result_delay_seconds_total": sum(delays),
            "result_delay_seconds_max": max(delays, default=0),
        }

    @staticmethod
    def _usable_reporting_sample(
        previous: dict[str, Any] | None, result: dict[str, Any]
    ) -> bool:
        """Only tightly bracketed first finals can teach future polling times."""
        if previous is None or previous["reporting_delay_seconds"] is not None:
            return False
        if previous["status"] != "SCHEDULED" or result["status"] != "FINAL":
            return False
        if any(
            result[key] is None or previous[key] is not None
            for key in ("home_score", "away_score")
        ):
            return False
        if (
            previous["result_observed_at"] is None
            or previous["starts_at"] != result["starts_at"]
            or previous.get("playing_time_minutes")
            != result.get("playing_time_minutes")
            or previous.get("pool__competition_class_id")
            != result.get("pool__competition_class_id")
        ):
            return False
        return (
            timedelta(0)
            < result["result_observed_at"] - previous["result_observed_at"]
            <= RESULT_INTERVAL
        )

    def completed(self, job: PollJob, *, checked: bool) -> None:
        """Avoid opponent-feed duplicates within the batch after a successful check."""
        if job.resource.kind in DETAIL_FIELDS:
            if checked and (row := self.current_by_source.get(job.resource.source_id)):
                self.metadata_changed.add(row["id"])
            return
        self.metadata_only_until = self.now
        self._completed_feed(job, checked=checked)

    def _completed_feed(self, job: PollJob, *, checked: bool) -> None:
        """Refresh only observed feed membership before planning overlapping checks."""
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
                    season=self.season, result_observed_at__gte=self.snapshot_at
                ).values_list("pk", flat=True)
            )
        if checked and job.resource.kind == "club_program":
            self.schedule_checked.update(job.covered_matches)
        if checked and job.resource.kind in {"club_results", "pool_results"}:
            self.checked.update(job.covered_matches)
        if checked and job.covered_matches:
            self._refresh_matches(job.covered_matches)
        if checked and job.resource.kind == "match_lineup":
            row = self.current_by_source.get(job.resource.source_id)
            self._refresh_matches({row["id"]} if row else set())
            self._apply_class_durations()

    def _refresh_matches(self, match_ids: set[int]) -> None:
        """Apply rescheduling before choosing the next feed."""
        fresh = {
            row["id"]: row
            for row in Match.objects.filter(
                season=self.season, pk__in=match_ids
            ).values(*MATCH_FIELDS)
        }
        for row in self.rows:
            if row["id"] in fresh:
                if (
                    row["pool__competition_class_id"]
                    != fresh[row["id"]]["pool__competition_class_id"]
                ):
                    row.pop("learned_delay_seconds", None)
                row.update(fresh[row["id"]])


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
    if job.resource.kind not in {"club_program", "club_results", "pool_results"}:
        return True
    field_name = (
        "schedule_checked_at"
        if job.resource.kind == "club_program"
        else "results_checked_at"
    )
    job.covered_matches = set(job.resource.match_ids)
    Match.objects.filter(season=job.resource.season, pk__in=job.covered_matches).filter(
        Q(**{field_name + "__isnull": True}) | Q(**{field_name + "__lt": now})
    ).update(**{field_name: now})
    return True
