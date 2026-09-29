"""Queue lossless repackaging for existing recordings (backfill)."""

from argparse import ArgumentParser

from django.core.management.base import BaseCommand

from apps.video_analysis.models import Recording
from apps.video_analysis.services import repackaging


class Command(BaseCommand):
    """List recordings; queue them on the vision worker with ``--execute``.

    Already compact files are detected from their index and left untouched.
    """

    help = __doc__

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Select recordings and require an explicit execution flag."""
        parser.add_argument("--recording", action="append", default=[])
        parser.add_argument("--execute", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Queue each selected recording's stored video once."""
        recordings = Recording.objects.all()
        if options["recording"]:
            recordings = recordings.filter(source_id__in=options["recording"])
        for recording in recordings.order_by("pk"):
            video = recording.metadata.get("video")
            if not video:
                continue
            if options["execute"]:
                repackaging.schedule(recording.workspace_id, video)
            verb = "queued" if options["execute"] else "would queue"
            self.stdout.write(f"{recording.source_id}: {verb} {video}")
