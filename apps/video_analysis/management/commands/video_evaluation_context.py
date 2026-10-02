"""Preview held-out-season evaluation groups for review recordings."""

from argparse import ArgumentParser
import json
from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand

from apps.video_analysis.services.evaluation_context import evaluation_report


class Command(BaseCommand):
    """Read-only: reports context coverage and a proposed split, never trains."""

    help = "Report season/discipline/format coverage and a held-out-season split."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Select a workspace and the editions to hold out."""
        parser.add_argument("--workspace", type=UUID)
        parser.add_argument(
            "--held-out-edition",
            action="append",
            default=[],
            type=int,
            help="Edition start year whose matches form the test set (repeatable)",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Write the JSON report to stdout."""
        values: dict[str, Any] = dict(options)
        report = evaluation_report(
            set(values["held_out_edition"]), values.get("workspace")
        )
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
