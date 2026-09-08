"""Enable a first roster pass without issuing provider requests in the command."""

from argparse import ArgumentParser

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.rosters import queue_rosters
from apps.schedule.models import Season


class Command(BaseCommand):
    """Queue currently known team variants behind existing import work."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require an explicit existing season."""
        parser.add_argument("--season", required=True)

    def handle(self, *args: object, **options: object) -> None:
        """Queue feeds with the existing budgets, checkpoint and retry policy.

        Raises:
            CommandError: The requested season is not current.

        """
        try:
            season = Season.objects.get(name=options["season"])
            count = queue_rosters(season)
        except (Season.DoesNotExist, ValueError) as exc:
            raise CommandError("An existing current season is required") from exc
        self.stdout.write(f"Queued {count} roster feeds")
