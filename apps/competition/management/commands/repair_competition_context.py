"""Preview or apply competition-context repairs for one provider scope."""

from argparse import ArgumentParser
import json
from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.context_repair import RepairOptions, run
from apps.game_tracker.composition import record_match_change
from apps.schedule.models import Season


# Poules per batch; timing checks follow the same poules.
MAX_LIMIT = 5000


class Command(BaseCommand):
    """Read-only by default; --apply writes one bounded, resumable batch."""

    help = (
        "Report (or with --apply repair) competition periods, outdoor phase "
        "routing, roster participations and match rule profiles of one scope."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require an explicit scope; every write needs --apply."""
        parser.add_argument("--scope", required=True, type=UUID)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--split-outdoor",
            action="store_true",
            help="Route independent autumn/spring poules to their own seasons",
        )
        parser.add_argument("--skip-timing", action="store_true")
        parser.add_argument(
            "--accept-reviewed",
            action="append",
            default=[],
            type=UUID,
            help="Native match whose pending rule correction was reviewed",
        )
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--after", type=int, default=0)

    def handle(self, *args: object, **options: object) -> None:
        """Write the JSON report; ``next_after`` resumes a partial run.

        Raises:
            CommandError: The scope is unknown or the repair cannot start.

        """
        values: dict[str, Any] = dict(options)
        if not 0 < values["limit"] <= MAX_LIMIT:
            raise CommandError(f"--limit must be between 1 and {MAX_LIMIT}")
        try:
            report = run(
                RepairOptions(
                    scope=Season.objects.get(pk=values["scope"]),
                    apply=values["apply"],
                    split_outdoor=values["split_outdoor"],
                    timing=not values["skip_timing"],
                    accept_reviewed=frozenset(values["accept_reviewed"]),
                    limit=values["limit"],
                    after=values["after"],
                    record_change=record_match_change,
                )
            )
        except (Season.DoesNotExist, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report.as_payload(), indent=2, sort_keys=True))
