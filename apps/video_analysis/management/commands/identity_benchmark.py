"""Create identity ground truth over a clip, or score clip runs against it."""

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.video_analysis.composition import worker_store
from apps.video_analysis.models import Workspace
from apps.video_analysis.services import identity_benchmark


class Command(BaseCommand):
    """``identity_benchmark create NAME --run ID [--every S]``.

    Or ``identity_benchmark evaluate NAME --run ID [--run ID ...]``.
    """

    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Declare the operation, benchmark name, runs and frame spacing."""
        parser.add_argument("operation", choices=["create", "evaluate"])
        parser.add_argument("name")
        parser.add_argument("--run", action="append", required=True)
        parser.add_argument(
            "--every", type=float, default=identity_benchmark.DEFAULT_EVERY
        )
        parser.add_argument("--workspace", default="main")
        parser.add_argument("--events", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        """Run the requested operation and print JSON."""
        workspace = Workspace.objects.get(slug=options["workspace"])
        store = worker_store(workspace, None, hydrate=False)
        if options["operation"] == "create":
            created = identity_benchmark.create(
                store, workspace, options["run"][0], options["name"], options["every"]
            )
            self.stdout.write(json.dumps(created, indent=2))
            return
        for run_id in options["run"]:
            report = identity_benchmark.evaluate(
                store, workspace, options["name"], run_id
            )
            if not options["events"]:
                report.pop("events")
            self.stdout.write(json.dumps(report, indent=2))
