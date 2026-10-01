"""Merge imported fixtures duplicating a manual match by a clock offset."""

from argparse import ArgumentParser
import json

from django.core.management.base import BaseCommand

from apps.competition.services.clock_twins import describe, find_pairs, merge


class Command(BaseCommand):
    """Default to a read-only preview; --apply merges the pairs."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require an explicit apply flag."""
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """List the pairs, and merge them when asked."""
        pairs = find_pairs()
        result: dict[str, object] = {"pairs": describe(pairs)}
        if options["apply"]:
            result["merged"] = merge(pairs)
        self.stdout.write(json.dumps(result, sort_keys=True))
