"""Enable a first roster pass without issuing provider requests in the command."""

from argparse import ArgumentParser
import json
from typing import cast

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.rosters import (
    RosterQueueSelection,
    apply_roster_plan,
    plan_rosters,
)
from apps.schedule.models import Season


class Command(BaseCommand):
    """Queue currently known team variants behind existing import work."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require an explicit existing season."""
        parser.add_argument("--season", required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without HTTP or writes (default)",
        )
        mode.add_argument(
            "--apply", action="store_true", help="Explicitly queue the selected feeds"
        )
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument(
            "--after", default="", help="Resume after this source team ID"
        )
        parser.add_argument("--sport", help="Select an exact provider SportId")
        parser.add_argument("--team-id", action="append", default=[])
        parser.add_argument(
            "--refresh-private",
            action="store_true",
            help="Refresh successful rosters containing private people",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Preview or explicitly queue a bounded selection behind existing work.

        Raises:
            CommandError: The requested season or bounded selection is invalid.

        """
        try:
            season = Season.objects.get(name=options["season"])
            plan = plan_rosters(
                season,
                refresh_private=bool(options["refresh_private"]),
                limit=int(str(options["limit"])),
                selection=RosterQueueSelection(
                    after=str(options["after"]),
                    sport=str(options["sport"]) if options["sport"] else None,
                    source_ids=tuple(cast(list[str], options["team_id"])),
                ),
            )
            report = plan.report()
            report.update(dry_run=not options["apply"], http_requests=0)
            report["queued"] = apply_roster_plan(plan) if options["apply"] else 0
        except (Season.DoesNotExist, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, sort_keys=True))
