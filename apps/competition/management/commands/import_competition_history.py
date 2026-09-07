"""Operate historical discovery with distinct provider identities."""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import date, timedelta
import json
import os
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.competition.adapters.outbound.history import HistoryClient
from apps.competition.composition import competition_client
from apps.competition.models import HistoricalResource
from apps.competition.services.history import progress, seed
from apps.competition.services.history_archive import import_archive
from apps.competition.services.history_worker import run_history
from apps.schedule.models import Season


class Command(BaseCommand):
    """Seed verified IDs, backfill explicit date ranges, and inspect coverage."""

    help = "Historical KNKV discovery: seed, run, status, retry, archive."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Expose bounded execution and protected credential file inputs."""
        parser.add_argument(
            "action", choices=("seed", "run", "status", "retry", "archive")
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
                publish=not options["no_publish"],
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
