"""Import an explicitly checked AI annotation report into native review storage."""

import json
from pathlib import Path
from typing import cast

from django.core.management.base import BaseCommand, CommandParser

from apps.video_analysis.composition import worker_store
from apps.video_analysis.engine.assisted_review import import_reviews
from apps.video_analysis.models import Workspace


class Command(BaseCommand):
    """Require an operator-supplied report and retain AI rather than human origin."""

    help = "Import visually checked AI annotations, preserving existing user reviews"

    def add_arguments(self, parser: CommandParser) -> None:
        """Require a report path and an explicit workspace."""
        parser.add_argument("report", type=Path)
        parser.add_argument("--workspace", default="main")

    def handle(self, *args: object, **options: object) -> None:
        """Import atomically; the audit actor stays unset for machine reviews."""
        workspace = Workspace.objects.get(slug=options["workspace"])
        store = worker_store(workspace, None)
        count = import_reviews(
            store, json.loads(cast(Path, options["report"]).read_text())
        )
        self.stdout.write(
            f"Imported {count} AI-reviewed frames; user reviews preserved"
        )
