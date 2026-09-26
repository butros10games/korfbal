"""Create a blind-check sample of AI-approved frames, or report its results."""

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.video_analysis.models import Workspace
from apps.video_analysis.services import blind_check


class Command(BaseCommand):
    """``blind_check create NAME [--size N]`` or ``blind_check report NAME``."""

    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Declare the operation, sample name and size."""
        parser.add_argument("operation", choices=["create", "report"])
        parser.add_argument("name")
        parser.add_argument("--size", type=int, default=blind_check.SAMPLE_SIZE)
        parser.add_argument("--workspace", default="main")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        """Run the requested operation."""
        workspace = Workspace.objects.get(slug=options["workspace"])
        if options["operation"] == "create":
            count = blind_check.create_sample(
                workspace, options["name"], options["size"]
            )
            self.stdout.write(f"Marked {count} frames for blind check")
        else:
            report = blind_check.report(workspace, options["name"])
            self.stdout.write(json.dumps(report, indent=2))
