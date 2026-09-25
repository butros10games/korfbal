"""Storage cutover contracts: private ranges, recovery, atomicity, and new samples."""

from http import HTTPStatus
import io
import json
from pathlib import Path
from typing import BinaryIO
from unittest.mock import ANY, MagicMock, patch
import uuid

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
import pytest
from pytest_django.fixtures import Settings

from apps.video_analysis.adapters.objects import WorkspaceObjects
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.composition import run_clip, worker_store
from apps.video_analysis.engine.media import sample_frame
from apps.video_analysis.engine.store import Store, frame_version
from apps.video_analysis.models import Frame, StoredFile, Workspace
from apps.video_analysis.services import uploads
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


@pytest.fixture
def s3() -> MagicMock:
    """Model immutable S3 bytes without hiding corruption behind local cache reads."""
    client = MagicMock()
    objects: dict[tuple[str, str], bytes] = {}

    def upload(handle: BinaryIO, bucket: str, key: str, **kwargs: object) -> None:
        objects[bucket, key] = handle.read()

    def get(**kwargs: str) -> dict[str, object]:
        content = objects[kwargs["Bucket"], kwargs["Key"]]
        if kwargs.get("Range"):
            start, end = map(int, kwargs["Range"].removeprefix("bytes=").split("-"))
            content = content[start : end + 1]
        return {"Body": io.BytesIO(content), "ContentLength": len(content)}

    def download(bucket: str, key: str, handle: BinaryIO) -> None:
        handle.write(objects[bucket, key])

    client.upload_fileobj.side_effect = upload
    client.get_object.side_effect = get
    client.download_fileobj.side_effect = download
    return client


def test_minio_primary_reads_and_verified_cache_recovery(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    s3: MagicMock,
) -> None:
    """Stream evicted media from MinIO and restore verified worker inputs."""
    owner, local, _ = imported
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    with patch("apps.video_analysis.adapters.objects.object_client", return_value=s3):
        call_command("migrate_video_storage", verify=True)
        records = list(StoredFile.objects.all())
        call_command("migrate_video_storage", verify=True)
        assert StoredFile.objects.count() == len(records)
        store = worker_store(records[0].workspace, owner)
        relative = store.read()["matches"][0]["frames"][0]["image"]
        path = local.media(relative)
        expected = path.read_bytes()
        path.unlink()
        client = verified(owner)
        response = client.get(
            "/video-analysis/media", {"path": relative}, HTTP_RANGE="bytes=0-9"
        )
        assert response.status_code == HTTPStatus.PARTIAL_CONTENT
        assert b"".join(response.streaming_content) == expected[:10]
        assert not path.exists(), "HTTP streaming must not hydrate a whole video"
        assert store.media(relative).read_bytes() == expected
        record = StoredFile.objects.get(relative_path=relative)
        assert record.bucket == settings.VIDEO_ANALYSIS_MEDIA_BUCKET
        assert record.object_key.startswith(
            f"video-analysis/{owner.pk}/{record.workspace.pk}/"
        )
        assert record.sha256 in record.object_key
        for invalid in ("../outside", "/absolute"):
            with pytest.raises(ValueError, match="Invalid workspace"):
                store.media(invalid)


def test_migration_restores_legacy_object_when_local_cache_is_gone(
    imported: tuple[User, DatabaseStore, Store], s3: MagicMock, settings: Settings
) -> None:
    """Existing indexed media must move even after its local cache was evicted."""
    _, local, _ = imported
    with patch("apps.video_analysis.adapters.objects.object_client", return_value=s3):
        call_command("migrate_video_storage", verify=True)
        relative = local.read()["matches"][0]["frames"][0]["image"]
        record = StoredFile.objects.get(relative_path=relative)
        path = local.media(relative)
        original = path.read_bytes()
        path.unlink()
        record.bucket = "old-video-media"
        record.object_key = "old-key"
        record.save(update_fields=["bucket", "object_key"])
        legacy = MagicMock()
        legacy.download_fileobj.side_effect = lambda bucket, key, output: output.write(
            original
        )
        legacy.get_object.return_value = {"Body": io.BytesIO(original[:4])}
        with patch(
            "apps.video_analysis.adapters.objects.legacy_object_client",
            return_value=legacy,
        ):
            pending = WorkspaceObjects(record.workspace, s3)
            assert b"".join(pending.chunks(relative, 0, 3)) == original[:4]
            call_command("migrate_video_storage", verify=True)
        record.refresh_from_db()
        assert record.bucket == settings.VIDEO_ANALYSIS_MEDIA_BUCKET
        assert record.object_key.startswith("video-analysis/")
        assert path.read_bytes() == original
        legacy.download_fileobj.assert_called_once_with(
            "old-video-media", "old-key", ANY
        )


def test_migration_refuses_corrupt_legacy_bytes(
    imported: tuple[User, DatabaseStore, Store], s3: MagicMock
) -> None:
    """An old object with the wrong hash must not become the new authoritative row."""
    _, local, _ = imported
    with patch("apps.video_analysis.adapters.objects.object_client", return_value=s3):
        call_command("migrate_video_storage", verify=True)
        relative = local.read()["matches"][0]["frames"][0]["image"]
        record = StoredFile.objects.get(relative_path=relative)
        path = local.media(relative)
        path.unlink()
        record.bucket = "old-video-media"
        record.object_key = "old-key"
        record.save(update_fields=["bucket", "object_key"])
        legacy = MagicMock()
        legacy.download_fileobj.side_effect = lambda bucket, key, output: output.write(
            b"corrupt"
        )
        with (
            patch(
                "apps.video_analysis.adapters.objects.legacy_object_client",
                return_value=legacy,
            ),
            pytest.raises(CommandError, match="checksum"),
        ):
            call_command("migrate_video_storage", verify=True)
        record.refresh_from_db()
        assert record.bucket == "old-video-media"
        assert not path.exists()


def test_corrupt_publication_rolls_back_review(
    imported: tuple[User, DatabaseStore, Store],
    s3: MagicMock,
) -> None:
    """A review is not acknowledged if its durable export fails verification."""
    owner, local, _ = imported
    workspace = local.workspace_id

    files = WorkspaceObjects(Workspace.objects.get(pk=workspace), s3)
    files.publish_review(local.read())
    store = DatabaseStore(files.workspace, owner, files)
    before = store.read()
    match = before["matches"][0]
    frame = match["frames"][0]
    s3.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(b"corrupt")}
    with pytest.raises(ValueError, match="verification failed"):
        store.update({
            "action": "review",
            "match_id": match["id"],
            "frame_id": frame["id"],
            "expected_frame_version": frame_version(frame),
            "annotation": dict(frame["proposal"], notes="must roll back"),
            "status": "approved",
            "complete": True,
        })
    assert store.read() == before


def test_new_sample_goes_to_django_and_minio(
    imported: tuple[User, DatabaseStore, Store],
    s3: MagicMock,
) -> None:
    """Extraction uses the persistence adapter, never the retired review.json writer."""
    owner, local, _ = imported

    files = WorkspaceObjects(Workspace.objects.get(pk=local.workspace_id), s3)
    store = DatabaseStore(files.workspace, owner, files)
    data = store.read()
    data["matches"][0]["video"] = "demo/recording.mp4"
    (store.root / "demo/recording.mp4").write_bytes(b"synthetic-video")
    with store.transaction():
        store._persist(data)
    count = Frame.objects.count()

    def extract(command: list[str], **kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"synthetic-frame")

    with (
        patch("apps.video_analysis.engine.media.binary", side_effect=lambda name: name),
        patch("apps.video_analysis.engine.media.subprocess.run", side_effect=extract),
    ):
        result = sample_frame(store, "demo", 800)
    assert Frame.objects.count() == count + 1
    assert Frame.objects.filter(source_id=result["frame_id"]).exists()
    assert not store.path.exists()
    record = StoredFile.objects.get(relative_path=f"demo/{result['frame_id']}.jpg")
    assert files.cache_media(record.relative_path).read_bytes() == b"synthetic-frame"
    latest = (
        StoredFile.objects
        .filter(relative_path__startswith="reviews/")
        .order_by("relative_path")
        .last()
    )
    assert latest is not None
    with s3.get_object(Bucket=latest.bucket, Key=latest.object_key)["Body"] as body:
        saved = json.load(body)
    assert len(saved["matches"][0]["frames"]) == count + 1


def test_metadata_hydration_does_not_download_model_weights(
    imported: tuple[User, DatabaseStore, Store], s3: MagicMock
) -> None:
    """Cold web requests recover JSON without restoring worker payloads."""
    _, local, _ = imported
    files = WorkspaceObjects(Workspace.objects.get(pk=local.workspace_id), s3)
    metadata = files.path("vision/runs/example/run.json")
    weights = files.path("vision/runs/example/fit/weights/best.pt")
    weights.parent.mkdir(parents=True)
    metadata.write_text('{"status":"completed"}')
    weights.write_bytes(b"synthetic checkpoint")
    for path in (metadata, weights):
        files.upload(path.relative_to(files.root).as_posix())
        path.unlink()
    files.hydrate_artifacts(metadata_only=True)
    assert metadata.is_file()
    assert not weights.exists()
    assert s3.download_fileobj.call_count == 1
    files.hydrate_artifacts()
    assert weights.read_bytes() == b"synthetic checkpoint"


def test_replay_restores_only_its_parent_and_stop_receipts(
    imported: tuple[User, DatabaseStore, Store], s3: MagicMock
) -> None:
    """Restore the authoritative replay boundary without hydrating unrelated files."""
    _, store, _ = imported
    workspace = Workspace.objects.get(pk=store.workspace_id)
    store.files = WorkspaceObjects(workspace, s3)
    paths = [
        "vision/clips/replay/run.json",
        "vision/clips/replay/cancel.json",
        "vision/clips/unrelated/run.json",
    ]
    for relative in paths:
        path = store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"synthetic": true}', encoding="utf-8")
        store.publish_artifact(relative)
        path.unlink()
    with patch("apps.video_analysis.composition.detector.clip"):
        run_clip(store, "replay", {"recording_end": 250})
    assert (store.root / paths[0]).is_file()
    assert (store.root / paths[1]).is_file()
    assert not (store.root / paths[2]).exists()


def test_upload_chunks_restore_privately_and_cleanup_preserves_recording_media(
    imported: tuple[User, DatabaseStore, Store],
    s3: MagicMock,
) -> None:
    """Temporary cleanup is scoped to one session and never exposes uploaded parts."""
    owner, _, _ = imported
    workspace = Workspace.objects.get(slug="main")
    files = WorkspaceObjects(workspace, s3)
    store = DatabaseStore(workspace, owner, files)
    data = b"\x00\x00\x00\x18ftyp" + b"synthetic"
    key = uploads.begin(
        workspace,
        owner,
        {"request_id": str(uuid.uuid4()), "name": "match.mp4", "size": len(data)},
    )["id"]
    uploads.part(workspace, owner, store, key=key, chunk=uploads.Chunk(0, data))
    record = StoredFile.objects.get(relative_path__startswith=f"uploads/{key}/")
    assert not (store.root / record.relative_path).exists()
    assert store.media(record.relative_path).read_bytes() == data
    client = verified(owner)
    assert (
        client.get("/video-analysis/media", {"path": record.relative_path}).status_code
        == HTTPStatus.NOT_FOUND
    )
    permanent = store.root / "recording.mp4"
    permanent.write_bytes(data)
    files.publish_media("recording.mp4")
    files.purge_upload(uuid.UUID(key))
    assert not StoredFile.objects.filter(pk=record.pk).exists()
    assert StoredFile.objects.filter(relative_path="recording.mp4").exists()
    s3.delete_object.assert_called_once_with(
        Bucket=record.bucket, Key=record.object_key
    )
