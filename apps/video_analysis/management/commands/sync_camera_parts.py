"""Sync a match video stitched from camera files using the files' start times."""

from argparse import ArgumentParser
from datetime import datetime
from pathlib import PurePosixPath
import subprocess
from typing import cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from django.contrib.auth.models import AnonymousUser
from django.core.management.base import BaseCommand, CommandError

from apps.game_tracker.models import MatchPart
from apps.schedule.models import Match
from apps.video_analysis.engine.media import probe_capture
from apps.video_analysis.models import MatchVideoPublication, Recording
from apps.video_analysis.services import camera_parts, match_video


def file_name(source: str) -> str:
    """Name a source without its query string, which may hold a signature.

    Returns:
        The file name.

    """
    return PurePosixPath(urlparse(source).path).name or "source"


def camera_file(source: str, zone: ZoneInfo) -> camera_parts.CameraFile:
    """Probe one original and read its start on the camera's own clock.

    Cameras write their local wall-clock time in a tag that ffprobe labels as
    UTC, so the digits are reinterpreted in the camera's time zone.

    Returns:
        The file's name, start and duration.

    """
    created, duration = probe_capture(source)
    wall = datetime.fromisoformat(created).replace(tzinfo=None)
    return camera_parts.CameraFile(
        name=file_name(source), started_at=wall.replace(tzinfo=zone), duration=duration
    )


class Command(BaseCommand):
    """Set recording breaks (and missing period starts) from the original files."""

    help = (
        "Pass the original camera files (paths or HTTPS URLs) of a stitched "
        "recording to set its recording breaks and missing period sync points."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Name the recording, its originals and how to read their clock."""
        parser.add_argument("recording", type=int, help="Recording primary key.")
        parser.add_argument("sources", nargs="+")
        parser.add_argument(
            "--camera-timezone",
            default="Europe/Amsterdam",
            help="Zone of the camera clock; use UTC for cameras that store UTC.",
        )
        parser.add_argument(
            "--max-gap",
            type=float,
            default=120.0,
            help="Longer gaps (half-time) get no break.",
        )
        parser.add_argument(
            "--anchors",
            choices=("missing", "all", "none"),
            default="missing",
            help="Which period sync points to set from the camera clock.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Report the derived sync and save it unless this is a dry run.

        Raises:
            CommandError: The recording or files cannot be synced.

        """
        del args
        recording, match = self._recording(options["recording"])
        zone = ZoneInfo(str(options["camera_timezone"]))
        files = [
            self._file(source, zone) for source in cast("list[str]", options["sources"])
        ]
        parts = list(MatchPart.objects.filter(match_data__match_link=match))
        try:
            sync = camera_parts.plan(
                files,
                duration=float(recording.metadata.get("duration_seconds") or 0),
                parts=parts,
                max_gap=float(str(options["max_gap"])),
            )
        except ValueError as error:
            raise CommandError(str(error)) from error
        publication = MatchVideoPublication.objects.filter(recording=recording).first()
        saved = publication.anchors if publication else {}
        mode = options["anchors"]
        anchors: dict[str, float | None] = {
            part_id: seconds
            for part_id, seconds in sync.anchors.items()
            if mode == "all" or (mode == "missing" and part_id not in saved)
        }
        self._report(files, sync, parts, saved, anchors)
        if options["dry_run"]:
            self.stdout.write("Dry run: nothing saved.")
            return
        match_video.update(
            match,
            AnonymousUser(),
            match_video.MatchVideoUpdate(
                expected_revision=publication.revision if publication else 0,
                breaks=sync.breaks,
                anchors=anchors or None,
            ),
        )
        self.stdout.write(f"Saved {len(sync.breaks)} recording breaks.")

    @staticmethod
    def _recording(pk: object) -> tuple[Recording, Match]:
        """Load a recording that is linked to a match.

        Returns:
            The recording and its match.

        Raises:
            CommandError: The recording does not exist or has no match.

        """
        recording = Recording.objects.select_related("match").filter(pk=pk).first()
        match = recording.match if recording else None
        if recording is None or match is None:
            raise CommandError("Unknown recording, or it is not linked to a match.")
        return recording, match

    @staticmethod
    def _file(source: str, zone: ZoneInfo) -> camera_parts.CameraFile:
        """Probe one original.

        Returns:
            The camera file.

        Raises:
            CommandError: The file cannot be probed or records no start time.

        """
        try:
            return camera_file(source, zone)
        except (ValueError, subprocess.SubprocessError) as error:
            raise CommandError(f"Cannot read {file_name(source)}.") from error

    def _report(
        self,
        files: list[camera_parts.CameraFile],
        sync: camera_parts.CameraSync,
        parts: list[MatchPart],
        saved: dict[str, float],
        anchors: dict[str, float | None],
    ) -> None:
        """Show the camera clock next to what is saved, so it can be checked."""
        for row in sorted(files, key=lambda item: item.started_at):
            start = row.started_at.isoformat()
            self.stdout.write(f"{row.name}: starts {start}, {row.duration:.3f} s")
        for join in sync.joins:
            kind = "break" if join.is_break else "no break (period change)"
            self.stdout.write(
                f"join at {join.video_seconds:.3f} s: "
                f"{join.gap_seconds:.3f} s missed, {kind}"
            )
        for part in parts:
            key = str(part.id_uuid)
            if key in sync.anchors:
                note = "set" if key in anchors else "kept"
                self.stdout.write(
                    f"period {part.part_number}: camera clock "
                    f"{sync.anchors[key]:.3f} s, saved {saved.get(key)} ({note})"
                )
