"""Queue the first match-selection pass without issuing provider requests."""

from argparse import ArgumentParser

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.lineups import queue_lineups
from apps.schedule.models import Season


class Command(BaseCommand):
    """Queue known match selections behind existing import work."""

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
            count = queue_lineups(season)
        except (Season.DoesNotExist, ValueError) as exc:
            raise CommandError("An existing season is required") from exc
        self.stdout.write(f"Queued {count} match selection feeds")
