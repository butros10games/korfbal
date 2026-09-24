"""Import an ordinary public Eyecons recording into the native review workspace."""

from argparse import ArgumentParser
from pathlib import Path
import tempfile

from django.core.management.base import BaseCommand

from apps.video_analysis.composition import worker_store
from apps.video_analysis.engine.eyecons import discover, download
from apps.video_analysis.engine.media import ImportOptions, import_recording
from apps.video_analysis.models import Workspace


class Command(BaseCommand):
    """Download footage without sampling pregame, halftime, or paused scenes."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Select an existing private workspace and a new recording ID."""
        parser.add_argument("url")
        parser.add_argument("--id", required=True)
        parser.add_argument("--slug", default="main")

    def handle(self, *args: object, **options: object) -> None:
        """Import footage and defer image creation until the React timeline is saved."""
        store = worker_store(Workspace.objects.get(slug=options["slug"]), None)
        source = discover(str(options["url"]))
        with tempfile.TemporaryDirectory(
            prefix="eyecons-", dir=store.root
        ) as temporary:
            video = Path(temporary) / "recording.mp4"
            download(source, video)
            import_recording(
                store,
                video,
                ImportOptions(
                    match_id=str(options["id"]),
                    title=source["title"],
                    source_url=source["source_url"],
                    split_group=f"eyecons-{source['external_id']}",
                    defer_frames=True,
                ),
            )
        self.stdout.write(
            "Recording ready. Mark active play in Videoanalyse → Video bewerken."
        )
