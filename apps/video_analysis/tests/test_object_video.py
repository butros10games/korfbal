"""Real range decoding, durable intake, and bounded worker-storage regressions."""

from collections.abc import Iterator
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeout,
)
import hashlib
from http import HTTPStatus
import io
from pathlib import Path
import shutil
import subprocess
from threading import Event
from typing import BinaryIO, cast
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
import pytest
from pytest_django.fixtures import Settings

from apps.video_analysis.adapters.object_video import PART_BYTES, upload_video
from apps.video_analysis.adapters.objects import WorkspaceObjects
from apps.video_analysis.adapters.pipeline import import_source
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.composition import processing_store
from apps.video_analysis.engine.media import sample_frame
from apps.video_analysis.engine.storage_workspace import (
    clear_incomplete_cache,
    storage_lease,
)
from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import Recording, StoredFile, Workspace
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


@pytest.fixture
def client() -> MagicMock:
    """Track multipart bytes and actual range reads, without a local media file."""
    client = MagicMock()
    objects, parts = {}, {}
    client.create_multipart_upload.return_value = {"UploadId": "upload"}

    def put(**kwargs: object) -> dict:
        parts[int(cast(int, kwargs["PartNumber"]))] = cast(bytes, kwargs["Body"])
        return {"ETag": str(kwargs["PartNumber"])}

    def complete(**kwargs: dict) -> None:
        objects[kwargs["Bucket"], kwargs["Key"]] = b"".join(
            parts[p["PartNumber"]] for p in kwargs["MultipartUpload"]["Parts"]
        )

    def get(**kwargs: str) -> dict:
        data = objects[kwargs["Bucket"], kwargs["Key"]]
        if kwargs.get("Range"):
            start, end = map(int, kwargs["Range"][6:].split("-"))
            data = data[start : end + 1]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}

    def upload(handle: BinaryIO, bucket: str, key: str, **kwargs: object) -> None:
        objects[bucket, key] = handle.read()

    client.upload_part.side_effect = put
    client.complete_multipart_upload.side_effect = complete
    client.get_object.side_effect = get
    client.upload_fileobj.side_effect = upload
    return client


@pytest.fixture
def video(tmp_path: Path) -> bytes:
    """Use an MP4 with its moov atom at the end so probing must seek."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "The media regression requires real FFmpeg"
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x90:r=5",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    return path.read_bytes()


def test_import_and_frame_extraction_never_stage_video(
    imported: tuple[User, DatabaseStore, Store], client: MagicMock, video: bytes
) -> None:
    """Verify import and frame extraction never stage video."""
    _, store, _ = imported
    workspace = Workspace.objects.get(slug="main")
    files = WorkspaceObjects(workspace, client)
    store.files = files
    recipe = {
        "match_id": "streamed",
        "source_url": "https://eyecons.com/videos/example",
    }
    source = {
        "title": "Synthetic",
        "source_url": recipe["source_url"],
        "external_id": "abcdefgh",
    }
    with (
        patch("apps.video_analysis.adapters.pipeline.discover", return_value=source),
        patch(
            "apps.video_analysis.adapters.pipeline.stream_download",
            side_effect=lambda _: (video[i : i + 64] for i in range(0, len(video), 64)),
        ),
    ):
        import_source(store, recipe)
        import_source(store, recipe)
    recording = Recording.objects.get(source_id="streamed")
    assert recording.metadata["video_sha256"] == hashlib.sha256(video).hexdigest()
    assert recording.metadata["width"] == int("160")
    assert not list(store.root.rglob("*.mp4"))
    assert client.create_multipart_upload.call_count == 1
    assert StoredFile.objects.filter(relative_path="streamed/recording.mp4").exists()
    recording.metadata["active_periods"] = [{"start": 0, "end": 2}]
    recording.save()
    result = sample_frame(store, "streamed", 1)
    assert result["frame_id"] == "at-000001000"
    assert not list(store.root.rglob("*.mp4"))
    assert any("Range" in call.kwargs for call in client.get_object.call_args_list)


def test_rejected_media_never_gets_indexed(
    imported: tuple[User, DatabaseStore, Store], client: MagicMock, video: bytes
) -> None:
    """Verify rejected media never gets indexed."""
    files = WorkspaceObjects(Workspace.objects.get(slug="main"), client)
    with pytest.raises(ValueError, match="signature"):
        files.import_video("invalid/recording.mp4", [b"invalid"])
    assert not StoredFile.objects.filter(relative_path="invalid/recording.mp4").exists()
    client.delete_object.assert_called_once()


def test_stream_failure_aborts_multipart(client: MagicMock) -> None:
    """Verify stream failure aborts multipart."""

    def chunks() -> Iterator[bytes]:
        yield b"first"
        raise OSError("source stopped")

    with pytest.raises(OSError, match="source stopped"):
        upload_video(client, "bucket", "key", chunks(), ".mp4")
    client.abort_multipart_upload.assert_called_once()
    client.complete_multipart_upload.assert_not_called()


def test_size_limit_aborts_before_publication(client: MagicMock) -> None:
    """Verify size limit aborts before publication."""
    with (
        patch("apps.video_analysis.adapters.object_video.MAX_BYTES", 3),
        pytest.raises(ValueError, match="6 GB"),
    ):
        upload_video(client, "bucket", "key", [b"1234"], ".mp4")
    client.abort_multipart_upload.assert_called_once()


def test_eviction_keeps_unpublished_and_modified_checkpoints(
    imported: tuple[User, DatabaseStore, Store], client: MagicMock
) -> None:
    """Verify eviction keeps unpublished and modified checkpoints."""
    _, store, _ = imported
    files = WorkspaceObjects(Workspace.objects.get(slug="main"), client)
    for name in ("best.pt", "last.pt"):
        path = store.root / "vision/runs/model" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"verified")
        files.publish_artifact(path.relative_to(store.root).as_posix())
    path.write_bytes(b"changed-output")
    pending = path.with_name("numbers.pt")
    pending.write_bytes(b"pending-output")
    files.evict_bulk()
    assert not path.with_name("best.pt").exists()
    assert path.read_bytes() == b"changed-output"
    assert pending.read_bytes() == b"pending-output"
    assert StoredFile.objects.filter(relative_path__endswith="best.pt").exists()


def test_direct_playback_authorizes_before_signing(
    imported: tuple[User, DatabaseStore, Store], client: MagicMock, settings: Settings
) -> None:
    """Verify direct playback authorizes before signing."""
    owner, store, _ = imported
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    files = WorkspaceObjects(Workspace.objects.get(slug="main"), client)
    relative = store.read()["matches"][0]["frames"][0]["image"]
    files.publish_media(relative)
    client.generate_presigned_url.return_value = (
        "https://media.example.test/private-image?signature=synthetic"
    )
    with patch(
        "apps.video_analysis.adapters.objects.object_client", return_value=client
    ):
        browser = verified(owner)
        response = browser.get(
            "/video-analysis/media", {"path": relative, "delivery": "direct"}
        )
        assert response.status_code == HTTPStatus.TEMPORARY_REDIRECT
        assert response["Location"].startswith("https://media.example.test/")
        assert response["Cache-Control"] == "private, no-store"
        assert (
            browser.get(
                "/video-analysis/media", {"path": "../other", "delivery": "direct"}
            ).status_code
            == HTTPStatus.NOT_FOUND
        )
        client.generate_presigned_url.assert_called_once()


def test_materialization_refuses_over_budget_without_downloading(
    imported: tuple[User, DatabaseStore, Store], client: MagicMock, settings: Settings
) -> None:
    """Space admission runs before any full object download starts."""
    _, store, _ = imported
    files = WorkspaceObjects(Workspace.objects.get(slug="main"), client)
    path = store.root / "vision/runs/model/best.pt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"checkpoint")
    files.publish_artifact(path.relative_to(store.root).as_posix())
    path.unlink()
    settings.VIDEO_ANALYSIS_WORKSPACE_MAX_BYTES = 1
    with pytest.raises(ValueError, match="staging budget"):
        files.cache_media(path.relative_to(store.root).as_posix())
    client.download_fileobj.assert_not_called()


def test_multipart_boundaries_preserve_every_byte(client: MagicMock) -> None:
    """Uneven source chunks must produce full nonfinal S3 parts without duplication."""
    content = b"A" * (PART_BYTES - 7) + b"B" * (PART_BYTES + 19)
    with patch(
        "apps.video_analysis.adapters.object_video.probe_object", return_value={}
    ):
        size, checksum, _ = upload_video(
            client, "bucket", "key", [content[:53], content[53:]], ".mp4"
        )
    assert size == len(content)
    assert checksum == hashlib.sha256(content).hexdigest()
    calls = client.upload_part.call_args_list
    assert all(len(call.kwargs["Body"]) == PART_BYTES for call in calls[:-1])


def test_worker_cleanup_retains_checkpoint_when_publication_fails(
    imported: tuple[User, DatabaseStore, Store], settings: Settings, client: MagicMock
) -> None:
    """A storage outage must never discard a newly produced trained checkpoint."""
    owner, _, _ = imported
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    workspace = Workspace.objects.get(slug="main")
    client.upload_fileobj.side_effect = OSError("storage unavailable")

    def produce() -> Path:
        with processing_store(workspace, owner) as working:
            checkpoint = working.root / "vision/runs/new/fit/weights/best.pt"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_bytes(b"trained model")
        return checkpoint

    with (
        patch(
            "apps.video_analysis.adapters.objects.object_client", return_value=client
        ),
        pytest.raises(OSError, match="unavailable"),
    ):
        produce()
    checkpoint = (
        Path(settings.VIDEO_ANALYSIS_ROOT)
        / str(owner.pk)
        / str(workspace.pk)
        / "vision/runs/new/fit/weights/best.pt"
    )
    assert checkpoint.read_bytes() == b"trained model"


def test_uploaded_parts_stream_without_local_assembly(
    imported: tuple[User, DatabaseStore, Store], client: MagicMock, video: bytes
) -> None:
    """Browser-uploaded chunks are verified from S3 without a full local copy."""
    _, store, _ = imported
    files = WorkspaceObjects(Workspace.objects.get(slug="main"), client)
    store.files = files
    parts = []
    for index, data in enumerate((video[:100], video[100:])):
        relative = f"uploads/session/{index}.part"
        path = files.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.publish_media(relative)
        path.unlink()
        parts.append({
            "path": relative,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    recipe = {
        "match_id": "uploaded-stream",
        "source_type": "upload",
        "upload": {"name": "game.mp4", "size": len(video), "parts": parts},
    }
    import_source(store, recipe)
    assert (
        Recording.objects.get(source_id="uploaded-stream").metadata["video_sha256"]
        == hashlib.sha256(video).hexdigest()
    )
    assert not list(store.root.rglob("*.mp4"))
    assert not list(store.root.rglob("*.part"))
    client.download_fileobj.assert_not_called()


def test_cache_cleanup_waits_for_active_download(tmp_path: Path) -> None:
    """An API metadata download must not be unlinked by worker startup cleanup."""
    started = Event()

    def cleanup() -> None:
        started.set()
        clear_incomplete_cache(tmp_path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with storage_lease(tmp_path, name=".cache.lock"):
            partial = tmp_path / ".cache-partial"
            partial.write_bytes(b"in flight")
            pending = pool.submit(cleanup)
            assert started.wait(timeout=5)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.05)
            assert partial.exists()
        pending.result(timeout=5)
    assert not partial.exists()
