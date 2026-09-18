"""Export authoritative review metadata for backup or an explicit rollback."""

from argparse import ArgumentParser
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.store import atomic_json
from apps.video_analysis.models import Workspace


class Command(BaseCommand):
    """Export without reopening a legacy writer or replacing an existing backup."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require an explicit backup path."""
        parser.add_argument("output", type=Path)
        parser.add_argument("--slug", default="main")

    def handle(self, *args: object, **options: object) -> None:
        """Write normalized metadata including all proposals and review history.

        Raises:
            CommandError: The destination already exists.

        """
        output = Path(str(options["output"]))
        if output.exists():
            raise CommandError("Backup already exists")
        store = DatabaseStore(Workspace.objects.get(slug=options["slug"]))
        atomic_json(output, store.read())
        self.stdout.write(
            "Review metadata exported; retain the workspace media alongside it."
        )
