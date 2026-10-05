"""Audit or locally backfill classifications with explicit reviewed overrides."""

from argparse import ArgumentParser
from collections import Counter
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from apps.competition.domain.classification import designation
from apps.competition.models import Match, Pool, PoolEntry, Team
from apps.competition.services.classification import (
    MAX_CLASS_BATCH,
    map_pool,
    plan_pool,
    relevel_classes,
)
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_edition


class Command(BaseCommand):
    """Default to a read-only report; never fetch upstream or publish fixtures."""

    help = "Audit competition mapping; --apply persists classifications only."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Scope by season name and source pool ID, with optional reviewed context."""
        parser.add_argument("--season", required=True)
        parser.add_argument("--pool", action="append")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--overrides", type=Path)
        parser.add_argument("--output", type=Path)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--after", type=int, default=0)
        parser.add_argument(
            "--relevel",
            action="store_true",
            help="Recompute existing class levels from their immutable keys only",
        )

    @transaction.atomic
    def handle(self, *args: object, **options: object) -> None:
        """Validate all overrides before applying any changes, then report coverage.

        Raises:
            CommandError: Review input is malformed or outside the selected scope.

        """
        parameters: dict[str, Any] = dict(options)
        if not 0 < parameters["limit"] <= MAX_CLASS_BATCH or parameters["after"] < 0:
            raise CommandError("--limit must be 1-5000 and --after nonnegative")
        if parameters["relevel"]:
            try:
                report = relevel_classes(
                    season=Season.objects.get(name=parameters["season"]),
                    apply=parameters["apply"],
                    limit=parameters["limit"],
                    after=parameters["after"],
                )
            except Season.DoesNotExist as exc:
                raise CommandError("Unknown season") from exc
            self._write_report(report, parameters["output"])
            return
        query = (
            Pool.objects
            .filter(season__name=parameters["season"], pk__gt=parameters["after"])
            .select_related("season", "competition_class__edition")
            .order_by("pk")
        )
        if parameters["pool"]:
            query = query.filter(external_id__in=parameters["pool"])
        if parameters["apply"]:
            query = query.select_for_update(of=("self",), no_key=True)
        rows = list(query[: parameters["limit"]])
        overrides = {}
        try:
            if parameters["overrides"]:
                overrides = json.loads(parameters["overrides"].read_text())
            decisions = review_decisions(rows, overrides)
        except (ValueError, OSError, TypeError) as error:
            raise CommandError(str(error)) from error
        report_rows, counts = mapping_report_rows(
            decisions, overrides, apply=parameters["apply"]
        )
        team_ids = set(
            PoolEntry.objects.filter(pool__in=rows).values_list("team_id", flat=True)
        )
        for home, away in Match.objects.filter(pool__in=rows).values_list(
            "home_team_id", "away_team_id"
        ):
            team_ids.update((home, away))
        teams = Team.objects.filter(pk__in=team_ids).select_related("season", "group")
        team_counts = Counter(
            designation(team.name, season_edition(team.season))["kind"]
            for team in teams
        )
        conflicts = list(
            PoolEntry.objects
            .filter(pool__in=rows)
            .exclude(team__season_id=models.F("pool__season_id"))
            .values_list("pk", flat=True)
        )
        report = {
            "season": parameters["season"],
            "applied": parameters["apply"],
            "scope": "Local discovered records only; provider completeness is unknown",
            "pools": len(rows),
            "next_after": rows[-1].pk if len(rows) == parameters["limit"] else None,
            "counts": dict(counts),
            "labels": dict(Counter(row.class_name for row in rows)),
            "team_designations": dict(team_counts),
            "unlinked_teams": teams.filter(group__local_team__isnull=True).count(),
            "cross_season_memberships": conflicts,
            "fixtures_without_pool": Match.objects.filter(
                season__name=parameters["season"], pool=None
            ).count(),
            "decisions": report_rows,
        }
        self._write_report(report, parameters["output"])

    def _write_report(self, report: dict, destination: Path | None) -> None:
        """Write the same concrete report to stdout or a requested file."""
        output = json.dumps(report, indent=2, sort_keys=True)
        if destination:
            destination.write_text(output + "\n")
        else:
            self.stdout.write(output)


def review_decisions(rows: list[Pool], overrides: dict) -> list:
    """Validate every reviewed override before any mutation.

    Raises:
        ValueError: An override is malformed or references an out-of-scope pool.

    """
    if not isinstance(overrides, dict) or set(overrides) - {
        row.external_id for row in rows
    }:
        raise ValueError(
            "Overrides must reference pools in the selected season and scope"
        )
    decisions = []
    for pool in rows:
        before = plan_pool(pool)
        if pool.external_id in overrides:
            review = overrides[pool.external_id]
            if (
                not isinstance(review, dict)
                or set(review) != {"values", "reason"}
                or not isinstance(review["reason"], str)
                or not review["reason"].strip()
                or not isinstance(review["values"], dict)
            ):
                raise ValueError(
                    "Override requires values and a non-empty review reason"
                )
            pool.mapping_override = {**review, "source": before["evidence"]}
        after = plan_pool(pool)
        decisions.append((pool, before, after))
    return decisions


def mapping_report_rows(
    decisions: list, overrides: dict, *, apply: bool
) -> tuple[list, Counter]:
    """Apply reviewed pool decisions and count their bounded outcomes."""
    report_rows = []
    counts = Counter()
    for pool, before, after in decisions:
        changed = (
            before != after
            or pool.mapping_version != after["version"]
            or pool.mapping_evidence != after["evidence"]
        )
        if apply:
            if pool.external_id in overrides:
                Pool.objects.filter(pk=pool.pk).update(
                    mapping_override=pool.mapping_override
                )
            result = map_pool(pool)
            changed = result["changed"]
        counts[after["status"]] += 1
        counts["changed"] += int(changed)
        report_rows.append({
            "pool": pool.external_id,
            "name": pool.name,
            "before": {
                "status": pool.mapping_status,
                "evidence": pool.mapping_evidence,
            },
            "after": after,
        })
    return report_rows, counts
