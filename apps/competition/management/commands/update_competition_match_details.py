"""Backfill missing playing time, venue and rules for an existing source season."""

from argparse import ArgumentParser
from datetime import date
import json
from typing import cast

from django.core.management.base import CommandError

from apps.competition.management.commands.sync_competition import Command as SyncCommand
from apps.competition.services.match_details import (
    DETAIL_FIELDS,
    DETAIL_STATES,
    MAX_DETAIL_SELECTION,
    DetailSelection,
    preview_details,
    queue_missing_details,
)
from apps.competition.services.monitoring import observe_run, outcome
from apps.competition.services.sync import MAX_REQUESTS, sync_details
from apps.schedule.models import Season


def _selection(options: dict[str, object]) -> DetailSelection:
    """Validate bounded scope before either preview or queue changes.

    Raises:
        CommandError: The limit, cursor or date range is invalid.

    """
    limit = int(str(options["limit"]))
    cursor = int(str(options["cursor"]))
    if not 1 <= limit <= MAX_DETAIL_SELECTION or cursor < 0:
        raise CommandError(
            "Limit must be between 1 and 1000; cursor must be nonnegative"
        )
    selection = DetailSelection(
        kinds=tuple(cast(list[str], options.get("component") or list(DETAIL_FIELDS))),
        states=tuple(
            cast(list[str], options.get("state") or ["unobserved", "stale", "empty"])
        ),
        source_ids=tuple(cast(list[str], options.get("source_id") or [])),
        statuses=tuple(cast(list[str], options.get("match_status") or [])),
        start_date=cast(date | None, options.get("from_date")),
        end_date=cast(date | None, options.get("to_date")),
        after=cursor,
        limit=limit,
    )
    if (
        selection.start_date
        and selection.end_date
        and selection.start_date > selection.end_date
    ):
        raise CommandError("from-date must not be after to-date")
    return selection


class Command(SyncCommand):
    """Reuse sync credentials and pacing while restricting this batch to details."""

    help = (
        "Backfill missing Sportlink match duration, venue and rules; rerun to resume."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Keep closed-edition metadata runs explicit, app-only and bounded."""
        super().add_arguments(parser)
        parser.add_argument(
            "--component", action="append", choices=tuple(DETAIL_FIELDS)
        )
        parser.add_argument("--state", action="append", choices=DETAIL_STATES)
        parser.add_argument(
            "--source-id",
            action="append",
            help="Exact PublicMatchId; repeat to select several",
        )
        parser.add_argument("--match-status", action="append")
        parser.add_argument("--from-date", type=date.fromisoformat)
        parser.add_argument("--to-date", type=date.fromisoformat)
        parser.add_argument("--cursor", type=int, default=0)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args: object, **options: object) -> None:
        """Preview locally or drain one bounded, durable metadata backfill batch.

        Raises:
            CommandError: Scope/credentials are invalid or a detail request failed.

        """
        try:
            season = Season.objects.get(name=options["season"])
            budget = int(str(options["max_requests"]))
            if not 1 <= budget <= MAX_REQUESTS:
                raise CommandError("Request budget must be between 1 and 10000")
        except (Season.DoesNotExist, ValueError) as exc:
            raise CommandError(
                "Provide an existing season and request budget from 1 to 10000"
            ) from exc
        selection = _selection(options)
        preview = preview_details(season, selection=selection)
        if options["dry_run"] or not preview["remaining_detail_requests"]:
            self.stdout.write(
                json.dumps(
                    {
                        "dry_run": bool(options["dry_run"]),
                        "http_requests": 0,
                        "max_http_requests": budget,
                        **preview,
                    },
                    sort_keys=True,
                )
            )
            return
        queued = queue_missing_details(season, selection=selection)
        try:

            def run() -> dict[str, object]:
                result = sync_details(
                    season,
                    client_factory=lambda: self._client(options),
                    budget=budget,
                    kinds=selection.kinds,
                    source_ids=tuple(preview["selected_source_ids"]),
                )
                state = outcome(result)
                if not result["requests"] and state == "completed":
                    state = "deferred"
                return {**result, "status": state}

            summary = observe_run(season, run)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "queued": queued,
                    **summary,
                    **preview_details(season, selection=selection),
                },
                sort_keys=True,
            )
        )
        if summary["exhausted"]:
            raise CommandError(
                "Import stalled: feeds exhausted their retries. Inspect diagnostics, "
                "fix the cause, then use retry_competition_resources "
                "for the affected kind."
            )
        if (
            not summary["requests"]
            and preview_details(season, selection=selection)[
                "remaining_detail_requests"
            ]
        ):
            raise CommandError(
                "Import deferred by retry backoff; no progress this batch. "
                "Retry after next_sync_at."
            )
        if summary["reauth_required"] or summary["failed"]:
            raise CommandError(
                "Some details could not be fetched; saved checkpoints allow resuming. "
                "Inspect SyncResource.last_error; renew the session if "
                "reauthentication is required."
            )
