"""Report context coverage and held-out-edition splits of a forecast export."""

from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.competition.offline.context_evaluation import (
    context_coverage,
    held_out_split,
)
from apps.competition.queries.forecast_export import CONTEXT_SCHEMA


class Command(BaseCommand):
    """Read a schema 3 export file; never fits, uploads or launches training."""

    help = "Summarize explicit context coverage and a held-out-edition split."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Read one export and optional held-out editions."""
        parser.add_argument("--input", required=True, type=Path)
        parser.add_argument(
            "--held-out-edition",
            action="append",
            default=[],
            type=int,
            help="Edition start year evaluated out of sample (repeatable)",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Write a JSON report to stdout.

        Raises:
            CommandError: The export predates explicit context.

        """
        values: dict[str, Any] = dict(options)
        report = json.loads(Path(values["input"]).read_text(encoding="utf-8"))
        if report.get("schema") != CONTEXT_SCHEMA:
            raise CommandError(
                "Export with --with-context (schema 3) for context evaluation"
            )
        rows = report["rows"] + report.get("prior_rows", [])
        result: dict[str, Any] = {"coverage": context_coverage(rows)}
        held_out = set(values["held_out_edition"])
        if held_out:
            split = held_out_split(rows, held_out)
            result["held_out"] = {
                "editions": sorted(held_out),
                **{name: len(part) for name, part in split.items()},
                "test_coverage": context_coverage(split["test"]),
            }
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
