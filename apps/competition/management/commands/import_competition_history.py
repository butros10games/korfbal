"""Operate historical discovery with distinct provider identities."""

from __future__ import annotations

from argparse import ArgumentParser
import csv
from datetime import date, timedelta
import json
import os
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.competition.adapters.outbound.history import HistoryClient
from apps.competition.composition import (
    competition_client,
    schedule_change_dispatcher,
)
from apps.competition.models import HistoricalResource
from apps.competition.services.history import progress, seed
from apps.competition.services.history_archive import import_archive
from apps.competition.services.history_editions import (
    LOG_FIELDS,
    edition_log,
    edition_summary,
    queue_edition_lineups,
    recheck_edition,
    seed_edition,
)
from apps.competition.services.history_sites import (
    KORFBALNL,
    UITSLAGEN,
    seed_site,
    site_summary,
)
from apps.competition.services.history_worker import run_history
from apps.competition.services.site_repair import repair_site
from apps.schedule.models import Season


class Command(BaseCommand):
    """Seed verified IDs, backfill explicit date ranges, and inspect coverage."""

    help = (
        "Historical KNKV discovery: edition, site, site-repair, recheck, lineups, "
        "seed, run, status, log, retry, archive."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Expose bounded execution and protected credential file inputs."""
        parser.add_argument(
            "action",
            choices=(
                "edition",
                "site",
                "site-repair",
                "recheck",
                "lineups",
                "seed",
                "run",
                "status",
                "log",
                "retry",
                "archive",
            ),
        )
        parser.add_argument(
            "--edition",
            type=int,
            nargs="+",
            help="Provider edition start year(s), e.g. 2024 for 2024-2025",
        )
        parser.add_argument(
            "--source",
            choices=(KORFBALNL, UITSLAGEN),
            help=(
                "Public result site for the site action: korfbalnl holds editions "
                "2016-2021, uitslagen holds 2025"
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="With site-repair: remove the duplicates instead of counting them",
        )
        parser.add_argument(
            "--output", type=Path, help="CSV file for the per-match import log"
        )
        parser.add_argument(
            "--teams",
            action="store_true",
            help="Discover poules through teams instead of scanning poule numbers",
        )
        parser.add_argument(
            "--all-seeds",
            action="store_true",
            help="With --teams, queue every catalogue team instead of probing",
        )
        parser.add_argument(
            "--season", help="Existing season name; dates are never guessed"
        )
        parser.add_argument("--provider", choices=("app", "dataservice"), default="app")
        parser.add_argument(
            "--kind", choices=("match", "pool", "window"), default="match"
        )
        parser.add_argument("--source-id")
        parser.add_argument("--start", type=date.fromisoformat)
        parser.add_argument("--end", type=date.fromisoformat)
        parser.add_argument(
            "--sport", default="", help="Verified SportId for Dataservice records"
        )
        parser.add_argument("--reference", default="operator")
        parser.add_argument("--session-file", type=Path)
        parser.add_argument(
            "--dataservice-file",
            type=Path,
            help="Mode-600 file containing the club Dataservice ID",
        )
        parser.add_argument("--max-requests", type=int, default=20)
        parser.add_argument(
            "--resource", type=int, help="Explicit checkpoint ID to retry or recheck"
        )
        parser.add_argument(
            "--file", type=Path, help="Normalized, attributed archive JSON"
        )
        parser.add_argument("--no-publish", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Report structured coverage and credential-free operational errors.

        Raises:
            CommandError: The operator input or credential configuration is invalid.

        """
        try:
            result = self.execute_action(options)
        except (ValueError, OSError, TypeError, KeyError, Season.DoesNotExist) as exc:
            raise CommandError(
                "Invalid history configuration or input; check season, dates, IDs "
                "and private credential files"
            ) from exc
        self.stdout.write(json.dumps(result, default=str, sort_keys=True))

    def execute_action(self, options: dict[str, Any]) -> dict | list:
        """Keep import, discovery and explicit retry operations resumable.

        Raises:
            ValueError: The supplied configuration or response is inconsistent.

        """
        action = options["action"]
        if (
            action in {"edition", "site", "site-repair", "recheck", "lineups", "log"}
            or (options["edition"])
        ):
            return edition_action(action, options)
        if action == "status":
            return progress()
        if action == "retry":
            if not options["resource"]:
                raise ValueError("Select an explicit resource")
            count = HistoricalResource.objects.filter(pk=options["resource"]).update(
                state="pending", reason="", attempts=0, next_attempt_at=timezone.now()
            )
            if not count:
                raise ValueError("Unknown resource")
            return {"retried": count}
        if action == "run":
            return run_history(
                lambda: history_client(options),
                budget=options["max_requests"],
                publish_with=(
                    None if options["no_publish"] else schedule_change_dispatcher()
                ),
            )
        season = Season.objects.get(name=options["season"])
        if action == "archive":
            if not options["file"]:
                raise ValueError("Archive file required")
            return import_archive(
                season, json.loads(options["file"].read_text(encoding="utf-8"))
            )
        if options["provider"] == "dataservice" and not options["sport"]:
            raise ValueError("A verified sport mapping is required")
        with transaction.atomic():
            resource = seed(
                season,
                options["provider"],
                options["kind"],
                options["source_id"],
                start=options["start"] or season.start_date,
                end=options["end"]
                or min(season.end_date, timezone.localdate() - timedelta(days=1)),
                sport=options["sport"],
                reference=options["reference"],
            )
        return {
            "resource": resource.pk,
            "state": resource.state,
            "coverage": resource.coverage,
        }


def history_client(options: dict[str, Any]) -> HistoryClient:
    """Load only protected credential files after claiming the provider lease.

    Raises:
        ValueError: The supplied configuration or response is inconsistent.

    """
    session = options["session_file"] or os.getenv("SPORTLINK_HISTORY_SESSION_FILE")
    dataservice = options["dataservice_file"] or os.getenv(
        "SPORTLINK_HISTORY_DATASERVICE_FILE"
    )
    client_id = ""
    if dataservice:
        path = Path(dataservice)
        if path.stat().st_mode & 0o077:
            raise ValueError("Private file required")
        client_id = path.read_text(encoding="utf-8").strip()
        if not client_id or any(c.isspace() for c in client_id):
            raise ValueError("Invalid client ID")
    app = competition_client("", Path(session)) if session else None
    return HistoryClient(app, client_id)


def edition_action(action: str, options: dict[str, Any]) -> dict | list:
    """Queue season-scoped editions, report their progress, or export their log.

    Raises:
        ValueError: The editions or output file are missing for the action.

    """
    editions = options["edition"] or []
    if not editions:
        raise ValueError("Select at least one edition")
    if action == "edition":
        return [
            seed_edition(
                edition, scan=not options["teams"], all_seeds=options["all_seeds"]
            )
            for edition in editions
        ]
    if action in {"site", "site-repair"}:
        return site_action(action, editions, options)
    if action == "recheck":
        return [recheck_edition(edition) for edition in editions]
    if action == "status":
        return [
            {**edition_summary(edition), "sites": site_summary(edition)}
            for edition in editions
        ]
    if action == "lineups":
        return [queue_edition_lineups(edition) for edition in editions]
    if action == "log" and len(editions) == 1 and options["output"]:
        return write_log(editions[0], options["output"])
    raise ValueError("Select one edition and an output file for the log")


def site_action(action: str, editions: list[int], options: dict[str, Any]) -> list:
    """Queue a public result site, or repair its copies of provider poules.

    Raises:
        ValueError: No result site was selected.

    """
    source = options["source"]
    if not source:
        raise ValueError("Select the public result site")
    if action == "site":
        return [seed_site(source, edition) for edition in editions]
    return [
        repair_site(source, edition, apply=options["apply"]) for edition in editions
    ]


def write_log(edition: int, output: Path) -> dict:
    """Write the per-match import log of one edition as CSV."""
    count = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
        writer.writeheader()
        for row in edition_log(edition):
            writer.writerow(row)
            count += 1
    return {"edition": edition, "rows": count, "output": str(output)}
