"""Explicit, resumable rollout of native season separation."""

from argparse import ArgumentParser
import json

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.season_repair import preview, repair
from apps.schedule.models import Season


class Command(BaseCommand):
    """Default to a read-only preview; --apply performs the stored-data repair."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require the known provider edition and an explicit apply flag."""
        parser.add_argument("--season", required=True)
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--refresh-rosters", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Report aggregate moves without exposing people or credentials.

        Raises:
            CommandError: The source scope or year is invalid.

        """
        try:
            scope = Season.objects.get(name=options["season"])
            result = (
                repair(
                    scope,
                    int(str(options["year"])),
                    refresh_rosters=bool(options["refresh_rosters"]),
                )
                if options["apply"]
                else preview(scope)
            )
        except (Season.DoesNotExist, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
