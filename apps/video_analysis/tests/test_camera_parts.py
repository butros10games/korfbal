"""Recording breaks and period starts from the original camera files."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
import pytest

from apps.game_tracker.models import MatchPart
from apps.schedule.tests import match_api_test_support as support
from apps.video_analysis.models import (
    MatchVideoPublication,
    Recording,
    StoredFile,
    Workspace,
)
from apps.video_analysis.services.camera_parts import CameraFile, plan


pytestmark = pytest.mark.django_db

# Apollo 3 - DTS 2 on 26 September 2026: four files, the camera clock on CEST.
FILES = {
    "SAM_2140.MP4": ("2026-09-26T15:08:18.000000Z", 914.247),
    "SAM_2141.MP4": ("2026-09-26T15:24:24.000000Z", 1006.339),
    "SAM_2142.MP4": ("2026-09-26T15:49:30.000000Z", 915.848),
    "SAM_2143.MP4": ("2026-09-26T16:04:48.000000Z", 917.884),
}
DURATION = 3754.317233
FIRST_HALF = datetime(2026, 9, 26, 13, 8, 34, 756000, tzinfo=UTC)
SECOND_HALF = datetime(2026, 9, 26, 13, 49, 30, 203000, tzinfo=UTC)
SAVED_REVISION = 2
EXPECTED_BREAKS = [(914.247, 51.753), (2836.434, 2.152)]


def camera_files() -> list[CameraFile]:
    """Build the originals with their camera-clock start converted to UTC.

    Returns:
        The files, deliberately out of order.

    """
    return [
        CameraFile(
            name=name,
            started_at=datetime.fromisoformat(created) - timedelta(hours=2),
            duration=duration,
        )
        for name, (created, duration) in reversed(FILES.items())
    ]


def match_parts() -> tuple[support.MatchGraph, list[MatchPart]]:
    """Create a match with both tracked halves.

    Returns:
        The match graph and its periods.

    """
    graph = support.create_match_graph(prefix="camera")
    parts = [
        MatchPart.objects.create(
            match_data=graph.match_data, part_number=number, start_time=start
        )
        for number, start in ((1, FIRST_HALF), (2, SECOND_HALF))
    ]
    return graph, parts


def test_plan_breaks_splits_and_finds_period_starts() -> None:
    """A split inside a half is a break; half-time is left to the sync point."""
    _, parts = match_parts()
    sync = plan(camera_files(), duration=DURATION, parts=parts, max_gap=120)
    assert sync.breaks == EXPECTED_BREAKS
    assert [join.is_break for join in sync.joins] == [True, False, True]
    assert sync.anchors == {
        str(parts[0].id_uuid): 16.756,
        str(parts[1].id_uuid): 1920.789,
    }


def test_plan_rejects_files_that_are_not_this_recording() -> None:
    """Missing or overlapping originals cannot be synced."""
    files = sorted(camera_files(), key=lambda row: row.started_at)
    with pytest.raises(ValueError, match="recording lasts"):
        plan(files[1:], duration=DURATION, parts=[], max_gap=120)
    overlapping = [
        *files[:3],
        replace(files[3], started_at=files[2].started_at + timedelta(seconds=10)),
    ]
    with pytest.raises(ValueError, match="starts before"):
        plan(overlapping, duration=DURATION, parts=[], max_gap=120)


def fake_probe(source: str) -> tuple[str, float]:
    """Return the recorded metadata of a known original.

    Returns:
        The creation time tag and duration.

    """
    return FILES[source.rsplit("/", 1)[-1].split("?", 1)[0]]


def test_command_saves_breaks_and_keeps_editor_sync_points() -> None:
    """The command reads the camera clock and keeps sync points an editor set."""
    graph, parts = match_parts()
    owner = support.create_user(username="camera-owner")
    recording = Recording.objects.create(
        workspace=Workspace.objects.create(slug="camera", owner=owner),
        source_id="camera",
        match=graph.match,
        metadata={"video": "camera/recording.mp4", "duration_seconds": DURATION},
    )
    StoredFile.objects.create(
        workspace=recording.workspace,
        relative_path="camera/recording.mp4",
        bucket="media",
        object_key="key/camera/recording.mp4",
        sha256="0" * 64,
        size=1,
    )
    edited = str(parts[0].id_uuid)
    MatchVideoPublication.objects.create(
        recording=recording, anchors={edited: 15.0}, revision=SAVED_REVISION
    )
    sources = [f"https://files.example.test/{name}?sig=secret" for name in FILES]
    output = StringIO()
    with patch(
        "apps.video_analysis.management.commands.sync_camera_parts.probe_capture",
        side_effect=fake_probe,
    ):
        call_command("sync_camera_parts", recording.pk, *sources, stdout=output)
    publication = MatchVideoPublication.objects.get(recording=recording)
    assert publication.breaks == [
        {"video_seconds": at, "skipped_seconds": skipped}
        for at, skipped in EXPECTED_BREAKS
    ]
    assert publication.anchors == {edited: 15.0, str(parts[1].id_uuid): 1920.789}
    assert publication.revision == SAVED_REVISION + 1
    assert "51.753 s missed" in output.getvalue()
    assert "secret" not in output.getvalue()


def test_command_dry_run_and_unreadable_files() -> None:
    """A dry run saves nothing; a file without a start time is refused."""
    graph, _ = match_parts()
    owner = support.create_user(username="camera-dry")
    recording = Recording.objects.create(
        workspace=Workspace.objects.create(slug="camera-dry", owner=owner),
        source_id="camera-dry",
        match=graph.match,
        metadata={"video": "camera/recording.mp4", "duration_seconds": DURATION},
    )
    with patch(
        "apps.video_analysis.management.commands.sync_camera_parts.probe_capture",
        side_effect=fake_probe,
    ):
        call_command(
            "sync_camera_parts", recording.pk, *FILES, "--dry-run", stdout=StringIO()
        )
    assert not MatchVideoPublication.objects.exists()
    with (
        patch(
            "apps.video_analysis.management.commands.sync_camera_parts.probe_capture",
            side_effect=ValueError("The file records no start time."),
        ),
        pytest.raises(CommandError, match=r"Cannot read SAM_2140\.MP4\."),
    ):
        call_command(
            "sync_camera_parts",
            recording.pk,
            "https://files.example.test/SAM_2140.MP4?sig=secret",
        )
