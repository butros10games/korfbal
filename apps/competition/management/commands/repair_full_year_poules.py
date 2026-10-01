"""Re-import poules split over both outdoor halves into the full-year season."""

from argparse import ArgumentParser
import json

from django.core.management.base import BaseCommand

from apps.competition.services.full_year_repair import repair_edition


class Command(BaseCommand):
    """Default to a read-only preview; --apply removes and requeues the poules."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Select editions and require an explicit apply flag."""
        parser.add_argument("--edition", type=int, nargs="+", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Report each edition's split poules and, when applied, the repair."""
        editions = options["edition"]
        assert isinstance(editions, list)
        result = [
            repair_edition(edition, apply=bool(options["apply"]))
            for edition in editions
        ]
        self.stdout.write(json.dumps(result, sort_keys=True))
