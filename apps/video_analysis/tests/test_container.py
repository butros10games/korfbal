"""Lossless repackaging: identical packets, compact index, one stored video."""

import hashlib
from pathlib import Path
import shutil
import subprocess
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone
import pytest
from pytest_django.fixtures import Settings

from apps.kwt_common.models import BackgroundJob
from apps.video_analysis.adapters.objects import WorkspaceObjects
from apps.video_analysis.adapters.repackaging import (
    delete_unreferenced,
    repackage_video,
)
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine import container
from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import (
    Recording,
    ReviewPipeline,
    StoredFile,
    Workspace,
)
from apps.video_analysis.services import pipeline_worker, repackaging
from apps.video_analysis.tests.test_object_video import client  # noqa: F401 - fixture


pytestmark = pytest.mark.django_db

RELATIVE = "match/recording.mp4"


def encode(path: Path, *arguments: str) -> None:
    """Write a synthetic recording with audio, interleaved like a download."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "The media regression requires real FFmpeg"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=25:d=20",
            "-f",
            "lavfi",
            "-i",
            "sine=d=20",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            *arguments,
            "-y",
            str(path),
        ],
        check=True,
        timeout=60,
    )


def reader(path: Path):  # noqa: ANN201 - local callable
    """Serve ranged reads from a local file.

    Returns:
        A range reader.

    """

    def read(start: int, end: int) -> bytes:
        with path.open("rb") as handle:
            handle.seek(start)
            return handle.read(end - start + 1)

    return read


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Provide a download-style MP4 with a frame-by-frame interleaved index.

    Returns:
        Its path.

    """
    path = tmp_path / "source.mp4"
    encode(path)
    return path


def test_repackaging_compacts_index_without_changing_packets(
    source: Path, tmp_path: Path
) -> None:
    """The regrouped file has a compact index and byte-identical streams."""
    target = tmp_path / "target.mp4"
    assert not container.index_is_compact(reader(source), source.stat().st_size)
    container.repackage(source, target)
    assert container.index_is_compact(reader(target), target.stat().st_size)
    container.verify_repackaged(source, target)


def test_verification_rejects_changed_media(source: Path, tmp_path: Path) -> None:
    """A re-encode or trim is never accepted as a repackaged copy."""
    changed = tmp_path / "changed.mp4"
    encode(changed, "-q:v", "2")
    with pytest.raises(ValueError, match="encoded media"):
        container.verify_repackaged(source, changed)


def test_index_at_the_end_is_not_compact(tmp_path: Path) -> None:
    """Files whose index follows the media always need repackaging."""
    path = tmp_path / "late.mp4"
    encode(path, "-movflags", "-faststart")
    assert not container.index_is_compact(reader(path), path.stat().st_size)


@pytest.fixture
def stored(
    source: Path,
    client: MagicMock,  # noqa: F811 - fixture
    settings: Settings,
    tmp_path: Path,
) -> tuple[WorkspaceObjects, bytes]:
    """Index the download-style recording as the workspace's only stored video.

    Returns:
        The workspace files adapter and the original bytes.

    """
    settings.VIDEO_ANALYSIS_MEDIA_BUCKET = "media"
    settings.VIDEO_ANALYSIS_ROOT = tmp_path / "native"
    owner = User.objects.create_user(username="owner")
    workspace = Workspace.objects.create(slug="main", owner=owner)
    data = source.read_bytes()
    client.objects["media", "original"] = data
    StoredFile.objects.create(
        workspace=workspace,
        relative_path=RELATIVE,
        bucket="media",
        object_key="original",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        source_mtime_ns=7,
    )
    return WorkspaceObjects(workspace, client), data


def test_stored_recording_is_replaced_once(
    stored: tuple[WorkspaceObjects, bytes],
    client: MagicMock,  # noqa: F811 - fixture
) -> None:
    """The verified copy replaces the only stored object; a rerun is a no-op."""
    files, data = stored
    assert repackage_video(files, RELATIVE) == "original"
    record = StoredFile.objects.get(relative_path=RELATIVE)
    stored = client.objects["media", record.object_key]
    assert record.sha256 == hashlib.sha256(stored).hexdigest()
    assert (record.size, record.source_mtime_ns) == (len(stored), 0)
    assert record.object_key.startswith(files.prefix + "/")
    assert record.object_key.endswith(RELATIVE)
    assert repackage_video(files, RELATIVE) is None
    job = BackgroundJob.objects.get(task=repackaging.DELETE)
    assert job.args == [str(files.workspace.pk), "original"]
    assert job.generation == 1
    assert not list(files.root.rglob("*.mp4"))
    # Running readers keep the superseded object until it is retired.
    assert ("media", "original") in client.objects
    assert not delete_unreferenced(files, record.object_key)
    assert not delete_unreferenced(files, "original")
    client.objects["media", files.prefix + "/old"] = data
    assert delete_unreferenced(files, files.prefix + "/old")
    assert ("media", files.prefix + "/old") not in client.objects


def test_cleanup_intent_failure_rolls_back_replacement_and_retry_recovers(
    stored: tuple[WorkspaceObjects, bytes],
    client: MagicMock,  # noqa: F811 - fixture
) -> None:
    """A failed cleanup enqueue cannot strand the original object on retry."""
    files, original = stored
    retire = repackaging.retire

    def interrupted_retirement(workspace_id: object, key: str) -> None:
        retire(workspace_id, key)
        raise RuntimeError("Interrupted cleanup scheduling")

    with (
        patch.object(repackaging, "retire", side_effect=interrupted_retirement),
        pytest.raises(RuntimeError, match="Interrupted cleanup scheduling"),
    ):
        repackage_video(files, RELATIVE)

    assert StoredFile.objects.get(relative_path=RELATIVE).object_key == "original"
    assert not BackgroundJob.objects.filter(task=repackaging.DELETE).exists()
    assert client.objects == {("media", "original"): original}

    assert repackage_video(files, RELATIVE) == "original"
    assert repackage_video(files, RELATIVE) is None
    job = BackgroundJob.objects.get(task=repackaging.DELETE)
    assert job.args == [str(files.workspace.pk), "original"]
    assert job.generation == 1


def test_import_and_command_queue_durable_repackaging(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Imports and the backfill command queue one vision job per recording."""
    _, _, _ = imported
    workspace = Workspace.objects.get(slug="main")
    recording = Recording.objects.get(source_id="demo")
    recording.metadata["video"] = "demo/recording.mp4"
    recording.save()
    run = ReviewPipeline.objects.create(
        workspace=workspace, recipe={"match_id": "demo", "stage": "import"}
    )
    with (
        patch.object(pipeline_worker, "pipeline_has_capacity", return_value=True),
        patch.object(pipeline_worker, "perform", return_value=({}, True)),
    ):
        pipeline_worker.advance(str(workspace.pk))
    run.refresh_from_db()
    assert run.status == "awaiting_cuts"
    key = f"{repackaging.REPACKAGE}:{workspace.pk}:demo/recording.mp4"
    job = BackgroundJob.objects.get(key=key)
    assert (job.queue, job.args) == (
        "vision",
        [str(workspace.pk), "demo/recording.mp4"],
    )
    call_command("repackage_recordings")
    assert BackgroundJob.objects.get(key=key).generation == job.generation
    call_command("repackage_recordings", "--execute")
    assert BackgroundJob.objects.get(key=key).generation == job.generation + 1


def test_superseded_object_is_retired_after_a_day() -> None:
    """Deletion is a one-shot job delayed past running readers."""
    before = timezone.now()
    repackaging.retire("workspace", "old-key")
    job = BackgroundJob.objects.get(key=f"{repackaging.DELETE}:workspace:old-key")
    assert job.due_at >= before + repackaging.SUPERSEDED_GRACE


def test_corrupt_download_keeps_the_original(
    stored: tuple[WorkspaceObjects, bytes],
    client: MagicMock,  # noqa: F811 - fixture
) -> None:
    """Bytes that do not match the indexed hash are never repackaged."""
    files, _ = stored
    StoredFile.objects.filter(relative_path=RELATIVE).update(sha256="0" * 64)
    with pytest.raises(ValueError, match="verification"):
        repackage_video(files, RELATIVE)
    assert StoredFile.objects.get(relative_path=RELATIVE).object_key == "original"
    assert ("media", "original") in client.objects
