"""Private recording intake and isolated CPU proposal generation."""

from collections.abc import Generator
from contextlib import suppress
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import BinaryIO, cast
import uuid

from django.conf import settings

from apps.video_analysis.engine.eyecons import discover, download, stream_download
from apps.video_analysis.engine.media import (
    ImportOptions,
    binary,
    import_recording,
    probe,
)
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.vision import artifact
from apps.video_analysis.file_storage import WorkspaceFiles


MAX_BATCH = 25
MAX_WIDTH = 7680
MAX_HEIGHT = 4320


def import_source(store: Store, recipe: dict) -> None:
    """Recover this intake's private destination without touching other recordings."""
    if any(m["id"] == recipe["match_id"] for m in store.read()["matches"]):
        return
    if getattr(store, "files", None):
        import_object_video(store, recipe)
        return
    destination = store.root / recipe["match_id"]
    # Only server-generated intake IDs reach this adapter. A killed copy may
    # leave a directory before the authoritative recording was committed.
    if destination.exists():
        shutil.rmtree(destination)
    if recipe.get("source_type") == "upload":
        import_uploaded(store, recipe)
        return
    source = discover(recipe["source_url"])
    with tempfile.TemporaryDirectory(prefix="intake-", dir=store.root) as temporary:
        video = Path(temporary) / "recording.mp4"
        download(source, video)
        import_recording(
            store,
            video,
            ImportOptions(
                match_id=recipe["match_id"],
                title=source["title"],
                source_url=source["source_url"],
                split_group=f"eyecons-{source['external_id']}",
                defer_frames=True,
            ),
        )


def import_object_video(store: Store, recipe: dict) -> None:
    """Keep accepted recordings in S3; only metadata enters the database."""
    files = cast(WorkspaceFiles, getattr(store, "files", None))
    upload = recipe.get("upload")
    source = {} if upload else discover(recipe["source_url"])
    suffix = Path(upload["name"]).suffix.lower() if upload else ".mp4"
    relative = f"{recipe['match_id']}/recording{suffix}"
    chunks = uploaded_chunks(files, upload) if upload else stream_download(source)
    try:
        metadata = files.import_video(relative, chunks)
    finally:
        chunks.close()
    store.add_match({
        "id": recipe["match_id"],
        "title": Path(upload["name"]).stem if upload else source["title"],
        "source_url": "" if upload else source["source_url"],
        "source_offset_seconds": 0,
        "match_start_seconds": None,
        "active_periods": [],
        "timeline_required": True,
        "split_group": f"uploaded-{metadata['video_sha256']}"
        if upload
        else f"eyecons-{source['external_id']}",
        "video": relative,
        "synthetic": False,
        "frames": [],
        **metadata,
    })


def uploaded_chunks(files: WorkspaceFiles, upload: dict) -> Generator[bytes]:
    """Verify every original upload chunk while streaming directly into its object.

    Yields:
        Bounded chunks from the private upload session.

    Raises:
        ValueError: A chunk or the assembled recording fails verification.

    """
    total = 0
    for part in upload["parts"]:
        digest, count = hashlib.sha256(), 0
        for chunk in files.chunks(part["path"], 0, part["size"] - 1):
            count += len(chunk)
            if count > part["size"]:
                raise ValueError("Stored upload chunk exceeds its size")
            digest.update(chunk)
            yield chunk
        if count != part["size"] or digest.hexdigest() != part["sha256"]:
            raise ValueError("Stored upload chunk verification failed")
        total += count
    if total != upload["size"]:
        raise ValueError("Uploaded recording size mismatch")


def infer_batch(store: Store, model: str, frames: list[dict], output: str) -> dict:
    """Persist reproducible drafts, bounding one CPU task to 25 frames.

    Raises:
        ValueError: The batch is empty or exceeds the bound.

    """
    if not 1 <= len(frames) <= MAX_BATCH:
        raise ValueError("Expected a bounded frame batch")
    target = store.root / output
    if target.is_file():
        store.publish_artifact(output)
        return json.loads(target.read_text())
    weights = artifact(store, "runs", model) / "fit/weights/best.pt"
    weights = store.media(weights.relative_to(store.root).as_posix())
    inputs = [{**frame, "path": str(store.media(frame["image"]))} for frame in frames]
    with tempfile.TemporaryDirectory(prefix="drafts-", dir=store.root) as temporary:
        source = Path(temporary) / "input.json"
        atomic_json(source, {"weights": str(weights), "model": model, "frames": inputs})
        subprocess.run(
            [
                settings.VIDEO_ANALYSIS_PYTHON,
                "-m",
                "apps.video_analysis.engine.pipeline_detect",
                str(source),
                str(target),
            ],
            check=True,
            timeout=1800,
        )
    store.publish_artifact(output)
    return json.loads(target.read_text())


def extract_batch(store: Store, match: dict, times: list[float]) -> list[dict]:
    """Decode bounded images outside database locks and persist media first.

    Raises:
        ValueError: A timestamp could not be decoded.

    """
    directory = store.root / Path(match["video"]).parent
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    with (
        store.video_source(match["video"]) as video,
        tempfile.TemporaryDirectory(
            prefix="pipeline-frames-", dir=directory
        ) as temporary,
    ):
        for time in times:
            frame_id = f"at-{round(time * 1000):09d}"
            image = Path(temporary) / f"{frame_id}.jpg"
            subprocess.run(
                [
                    binary("ffmpeg"),
                    "-v",
                    "error",
                    "-ss",
                    str(time),
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(image),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            if not image.is_file():
                raise ValueError("No decodable frame in the selected section")
            target = directory / image.name
            image.replace(target)
            relative = target.relative_to(store.root).as_posix()
            store.publish_media(relative)
            rows.append({
                "id": frame_id,
                "time_seconds": time,
                "source_time_seconds": match.get("source_offset_seconds", 0) + time,
                "image": relative,
            })
    return rows


def has_capacity(store: Store, *, importing: bool) -> bool:
    """Reserve local staging space; queue work instead of filling the worker disk."""
    if importing and getattr(store, "files", None):
        return True  # Multipart streaming and remote probing never stage a video.
    required = (
        getattr(settings, "VIDEO_ANALYSIS_PIPELINE_IMPORT_FREE_BYTES", 13_000_000_000)
        if importing
        else getattr(settings, "VIDEO_ANALYSIS_PIPELINE_WORK_FREE_BYTES", 2_000_000_000)
    )
    return shutil.disk_usage(store.root).free >= required


def import_uploaded(store: Store, recipe: dict) -> None:
    """Assemble verified private chunks, inspect real media, then publish a recording.

    Raises:
        ValueError: Stored bytes or decoded media violate the upload contract.

    """
    upload = recipe["upload"]
    with tempfile.TemporaryDirectory(prefix="uploaded-", dir=store.root) as temporary:
        video = Path(temporary) / ("recording" + Path(upload["name"]).suffix.lower())
        total = 0
        with video.open("wb") as target:
            for part in upload["parts"]:
                source = store.media(part["path"])
                total += copy_part(source, target, part)
                if getattr(store, "files", None):
                    source.unlink(missing_ok=True)
        if total != upload["size"]:
            raise ValueError("Uploaded recording size mismatch")
        with video.open("rb") as content:
            source_checksum = hashlib.file_digest(content, "sha256").hexdigest()
        metadata = probe(video)
        if not (
            0 < metadata["duration_seconds"] <= 8 * 3600
            and 0 < metadata["width"] <= MAX_WIDTH
            and 0 < metadata["height"] <= MAX_HEIGHT
        ):
            raise ValueError("Choose a playable video up to eight hours and 8K")
        import_recording(
            store,
            video,
            ImportOptions(
                match_id=recipe["match_id"],
                title=Path(upload["name"]).stem,
                split_group=f"uploaded-{source_checksum}",
                defer_frames=True,
            ),
        )


def purge_uploaded_chunks(store: Store, upload_id: uuid.UUID) -> None:
    """Remove only this intake's temporary private chunks, never recording media."""
    upload_id = uuid.UUID(str(upload_id))
    files = getattr(store, "files", None)
    if files:
        files.purge_upload(upload_id)
    with suppress(FileNotFoundError):
        shutil.rmtree(store.root / "uploads" / str(upload_id))


def copy_part(source: Path, target: BinaryIO, part: dict) -> int:
    """Bound and verify a durable chunk while assembling the worker input.

    Raises:
        ValueError: A chunk differs from its accepted immutable bytes.

    """
    checksum, count = hashlib.sha256(), 0
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            count += len(chunk)
            if count > part["size"]:
                raise ValueError("Stored upload chunk exceeds its size")
            checksum.update(chunk)
            target.write(chunk)
    if count != part["size"] or checksum.hexdigest() != part["sha256"]:
        raise ValueError("Stored upload chunk verification failed")
    return count
