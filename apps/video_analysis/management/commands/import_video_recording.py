"""Import additional local footage directly into the native review workspace."""

from argparse import ArgumentParser
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.video_analysis.composition import worker_store
from apps.video_analysis.engine.media import ImportOptions, import_recording
from apps.video_analysis.models import Workspace


class Command(BaseCommand):
    """Run bounded extraction on a worker machine with ffmpeg installed."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Keep extraction size and recording identity explicit."""
        parser.add_argument("source", type=Path)
        parser.add_argument("--id", required=True)
        parser.add_argument("--title", required=True)
        parser.add_argument("--slug", default="main")
        parser.add_argument("--start", type=float, default=0)
        parser.add_argument("--interval", type=float, default=10)
        parser.add_argument("--count", type=int, default=24)

    def handle(self, *args: object, **options: object) -> None:
        """Append a recording using the same validation as legacy extraction."""
        store = worker_store(Workspace.objects.get(slug=options["slug"]), None)
        config = ImportOptions(
            match_id=str(options["id"]),
            title=str(options["title"]),
            start=float(str(options["start"])),
            interval=float(str(options["interval"])),
            count=int(str(options["count"])),
        )
        import_recording(store, Path(str(options["source"])), config)
        self.stdout.write("Recording imported into the native review queue.")
