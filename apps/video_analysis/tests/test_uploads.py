"""Uploaded recordings retain ownership, retry boundaries and durable review intent."""

from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
import uuid

from django.contrib.auth.models import User
from django.utils import timezone
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.store import ConflictError, Store
from apps.video_analysis.models import Recording, ReviewPipeline, VideoUpload, Workspace
from apps.video_analysis.services import pipeline, pipeline_worker, uploads
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


def start(owner: User, workspace: Workspace, data: bytes) -> str:
    """Create an explicitly declared synthetic upload."""
    return uploads.begin(
        workspace,
        owner,
        {"request_id": str(uuid.uuid4()), "name": "match.mp4", "size": len(data)},
    )["id"]


def test_retry_finish_and_cleanup_preserve_the_import(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Lost acknowledgements never duplicate chunks or enqueue duplicate imports."""
    owner, store, _ = imported
    workspace = Workspace.objects.get(slug="main")
    data = b"\x00\x00\x00\x18ftyp" + b"synthetic-content"
    key = start(owner, workspace, data)
    chunk = uploads.Chunk(0, data)
    first = uploads.part(workspace, owner, store, key=key, chunk=chunk)
    assert uploads.part(workspace, owner, store, key=key, chunk=chunk) == first
    with pytest.raises(ConflictError):
        uploads.part(
            workspace, owner, store, key=key, chunk=uploads.Chunk(0, data[:-1] + b"x")
        )
    uploads.finish(workspace, owner, key)
    uploads.finish(workspace, owner, key)
    assert ReviewPipeline.objects.filter(pk=key).count() == 1
    run = ReviewPipeline.objects.get(pk=key)
    assert run.recipe["upload"]["parts"][0]["size"] == len(data)
    with pytest.raises(ConflictError):
        uploads.cancel(workspace, owner, key)
    uploads.cleanup(key)
    assert VideoUpload.objects.get(pk=key).parts
    run.status = "awaiting_cuts"
    run.save()
    uploads.cleanup(key)
    assert VideoUpload.objects.get(pk=key).status == "consumed"
    assert not (store.root / "uploads" / key).exists()
    assert uploads.finish(workspace, owner, key)["status"] == "consumed"


def test_file_declarations_and_account_scope(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Neither a staff peer nor an arbitrary path can mutate another upload."""
    owner, store, _ = imported
    workspace = Workspace.objects.get(slug="main")
    key = start(owner, workspace, b"tiny")
    other = User.objects.create_user(username="other", is_staff=True)
    with pytest.raises(VideoUpload.DoesNotExist):
        uploads.part(workspace, other, store, key=key, chunk=uploads.Chunk(0, b"tiny"))
    with pytest.raises(ValueError, match="contents"):
        uploads.part(workspace, owner, store, key=key, chunk=uploads.Chunk(0, b"tiny"))
    with pytest.raises(ValueError, match="entire file"):
        uploads.finish(workspace, owner, key)
    for name, size in [
        ("../match.mp4", 100),
        ("match.html", 100),
        ("match.mp4", True),
        ("match.mp4", uploads.MAX_SIZE + 1),
        ("match.mp4", 0),
    ]:
        with pytest.raises(ValueError, match="Choose"):
            uploads.begin(
                workspace,
                owner,
                {"request_id": str(uuid.uuid4()), "name": name, "size": size},
            )
    with pytest.raises(ConflictError):
        uploads.begin(
            workspace, other, {"request_id": key, "name": "match.mp4", "size": 4}
        )


@pytest.mark.parametrize("peer", [False, True], ids=["same-account", "staff-peer"])
def test_preparation_cannot_reuse_an_open_upload_request_id(
    imported: tuple[User, DatabaseStore, Store], peer: bool
) -> None:
    """A colliding URL intake cannot take an upload's ID and break its completion."""
    owner, store, _ = imported
    workspace = Workspace.objects.get(slug="main")
    data = b"\0\0\0\x18ftyp" + b"synthetic-video"
    key = start(owner, workspace, data)
    actor = User.objects.create_user(username="peer", is_staff=True) if peer else owner

    with pytest.raises(ConflictError, match="upload"):
        pipeline.submit(
            workspace,
            actor,
            store,
            {"request_id": key, "source_url": "https://eyecons.com/videos/match"},
        )

    assert not ReviewPipeline.objects.filter(pk=key).exists()
    uploads.part(workspace, owner, store, key=key, chunk=uploads.Chunk(0, data))
    assert uploads.finish(workspace, owner, key)["status"] == "queued"
    assert ReviewPipeline.objects.get(pk=key).recipe["source_type"] == "upload"


def test_chunks_are_sequential_and_expire(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Missing, oversized and late chunks cannot be admitted or queued."""
    owner, store, _ = imported
    workspace = Workspace.objects.get(slug="main")
    data = b"\0\0\0\x18ftyp" + b"x" * 16
    with patch.object(uploads, "CHUNK_SIZE", 16):
        key = start(owner, workspace, data)
        with pytest.raises(ValueError, match="order"):
            uploads.part(
                workspace, owner, store, key=key, chunk=uploads.Chunk(1, data[16:])
            )
        with pytest.raises(ValueError, match="size"):
            uploads.part(workspace, owner, store, key=key, chunk=uploads.Chunk(0, data))
        uploads.part(
            workspace, owner, store, key=key, chunk=uploads.Chunk(0, data[:16])
        )
        uploads.part(
            workspace, owner, store, key=key, chunk=uploads.Chunk(1, data[16:])
        )
        assert VideoUpload.objects.get(pk=key).received == len(data)
    VideoUpload.objects.filter(pk=key).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(ConflictError):
        uploads.finish(workspace, owner, key)
    uploads.cleanup(key)
    assert VideoUpload.objects.get(pk=key).status == "expired"
    assert BackgroundJob.objects.filter(
        key=f"apps.video_analysis.tasks.cleanup_upload:{key}"
    ).exists()


def test_real_uploaded_video_reaches_cutting_and_rejects_corrupt_media(
    imported: tuple[User, DatabaseStore, Store],
    tmp_path: Path,
) -> None:
    """Exercise ffprobe, verified assembly and native recording persistence."""
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("ffmpeg is unavailable")
    owner, store, _ = imported
    workspace = Workspace.objects.get(slug="main")
    video = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x90:r=5",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
        timeout=30,
    )
    data = video.read_bytes()
    key = start(owner, workspace, data)
    uploads.part(workspace, owner, store, key=key, chunk=uploads.Chunk(0, data))
    uploads.finish(workspace, owner, key)
    with patch.object(pipeline_worker, "pipeline_has_capacity", return_value=True):
        pipeline_worker.advance(str(workspace.pk))
    run = ReviewPipeline.objects.get(pk=key)
    assert run.status == "awaiting_cuts"
    recording = Recording.objects.get(source_id=run.recipe["match_id"])
    assert recording.metadata["timeline_required"]
    assert recording.metadata["width"] == int("160")
    assert not recording.frames.exists()
    assert recording.metadata["title"] == "match"
    assert (
        recording.metadata["split_group"]
        == f"uploaded-{recording.metadata['video_sha256']}"
    )
    corrupt = b"\0\0\0\x18ftypnot-a-real-video"
    bad = start(owner, workspace, corrupt)
    uploads.part(workspace, owner, store, key=bad, chunk=uploads.Chunk(0, corrupt))
    uploads.finish(workspace, owner, bad)
    with patch.object(pipeline_worker, "pipeline_has_capacity", return_value=True):
        pipeline_worker.advance(str(workspace.pk))
    assert ReviewPipeline.objects.get(pk=bad).status == "failed"
    assert not Recording.objects.filter(
        source_id=f"intake-{uuid.UUID(bad).hex}"
    ).exists()


def test_transport_requires_mfa_csrf_and_bounded_chunks(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The binary endpoint keeps the same session/CSRF boundary as review writes."""
    owner, _, _ = imported
    client = verified(owner)
    response = client.get("/video-analysis/pipeline")
    csrf = response.json()["csrf"]
    body = {"request_id": str(uuid.uuid4()), "name": "match.mp4", "size": 8}
    assert (
        client.post(
            "/video-analysis/pipeline/upload", body, content_type="application/json"
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    response = client.post(
        "/video-analysis/pipeline/upload",
        body,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert response.status_code == HTTPStatus.OK
    key = response.json()["id"]
    response = client.post(
        f"/video-analysis/pipeline/upload/part?id={key}&index=0",
        b"\0\0\0\x18ftyp",
        content_type="application/octet-stream",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert response.status_code == HTTPStatus.OK
    session = client.session
    session.pop("bg_auth_mfa_verified")
    session.save()
    assert (
        client.post(
            "/video-analysis/pipeline/upload/finish",
            {"id": key},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
