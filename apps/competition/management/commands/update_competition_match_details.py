"""Backfill missing playing time, venue and rules for an existing source season."""

import json

from django.core.management.base import CommandError

from apps.competition.management.commands.sync_competition import Command as SyncCommand
from apps.competition.services.match_details import (
    preview_details,
    queue_missing_details,
)
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
            summary = sync_details(
                season,
                client_factory=lambda: self._client(options),
                budget=budget,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {"queued": queued, **summary, **preview_details(season)}, sort_keys=True
            )
        )
        if summary["reauth_required"] or summary["failed"]:
            raise CommandError(
                "Some details could not be fetched; saved checkpoints allow resuming. "
                "Inspect SyncResource.last_error; renew the session if "
                "reauthentication is required."
            )
