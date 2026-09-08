"""Import the KNKV allocation spreadsheet CSV with provenance and a dry-run report."""

from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.allocations import import_allocations
from apps.schedule.models import Season


class Command(BaseCommand):
    """Stage all source rows locally; uncertain identity links stay unresolved."""

    help = (
        "Read KNKV two-column CSV; --apply stages rows and links exact existing teams."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require explicit season; worksheet gender must not be inferred from towns."""
        parser.add_argument("--season", required=True)
        parser.add_argument("--edition", choices=("outdoor-autumn",), required=True)
        parser.add_argument("--file", type=Path, required=True)
        parser.add_argument("--label", required=True)
        parser.add_argument(
            "--gender", choices=("unknown", "mixed", "women"), default="unknown"
        )
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--output", type=Path)

    def handle(self, *args: object, **options: object) -> None:
        """Read once, validate all rows, and persist only on explicit apply.

        Raises:
            CommandError: The input or selected season is invalid.

        """
        parameters: dict[str, Any] = dict(options)
        try:
            season = Season.objects.get(name=parameters["season"])
            report = import_allocations(
                parameters["file"].read_bytes(),
                season,
                apply=parameters["apply"],
                label=parameters["label"],
                gender=parameters["gender"],
            )
        except (
            OSError,
            ValueError,
            Season.DoesNotExist,
            Season.MultipleObjectsReturned,
        ) as error:
            raise CommandError(str(error)) from error
        output = json.dumps(report, indent=2, sort_keys=True)
        if parameters["output"]:
            parameters["output"].write_text(output + "\n")
        else:
            self.stdout.write(output)
