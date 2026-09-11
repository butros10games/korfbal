"""Backfill missing playing time, venue and rules for an existing source season."""

import json

from django.core.management.base import CommandError

from apps.competition.management.commands.sync_competition import Command as SyncCommand
from apps.competition.services.match_details import (
    preview_details,
    queue_missing_details,
)
from apps.competition.services.monitoring import observe_run, outcome
from apps.competition.services.sync import MAX_REQUESTS, sync_details
from apps.schedule.models import Season


class Command(SyncCommand):
    """Reuse sync credentials and pacing while restricting this batch to details."""

    help = (
        "Backfill missing Sportlink match duration, venue and rules; rerun to resume."
    )

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
        preview = preview_details(season)
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
        queued = queue_missing_details(season)
        try:

            def run() -> dict[str, object]:
                result = sync_details(
                    season,
                    client_factory=lambda: self._client(options),
                    budget=budget,
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
                {"queued": queued, **summary, **preview_details(season)}, sort_keys=True
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
            and preview_details(season)["remaining_detail_requests"]
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
