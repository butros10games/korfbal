"""Native clip requests and owner-scoped, bounded result reads."""

from contextlib import suppress
from dataclasses import asdict
import json
from pathlib import Path
import uuid

from django.contrib.auth.models import User

from apps.video_analysis.engine.clip_contract import ClipOptions
from apps.video_analysis.engine.clip_models import MODEL_ERROR, supports_clips
from apps.video_analysis.engine.clips import directory
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.vision import artifact
from apps.video_analysis.models import AnalysisJob, Recording, StoredFile, Workspace
from apps.video_analysis.services.jobs import schedule


def read_file(store: Store, workspace: Workspace, path: Path) -> dict:
    """Hydrate just this private artifact, never every checkpoint or clip chunk."""
    if (
        not path.is_file()
        and StoredFile.objects.filter(
            workspace=workspace, relative_path=path.relative_to(store.root).as_posix()
        ).exists()
    ):
        store.media(path.relative_to(store.root).as_posix())
    return json.loads(path.read_text(encoding="utf-8"))


def start(
    store: Store, workspace: Workspace, actor: User, payload: dict
) -> AnalysisJob:
    """Validate a CPU-only job with an idempotent request ID.

    Raises:
        ValueError: The request or input does not satisfy this operation.

    """
    options = ClipOptions.parse(payload.get("options", {}))
    recording = Recording.objects.filter(
        workspace=workspace, source_id=payload["match_id"]
    ).first()
    if recording is None:
        raise ValueError("Choose a recording from this workspace")
    options.for_recording(recording.metadata)
    root = artifact(store, "runs", payload["model"])
    model = read_file(store, workspace, root / "run.json")
    if model.get("kind") != "train" or model.get("status") != "completed":
        raise ValueError("Choose a completed Korfbal training run")
    classes = model.get("classes")
    if classes is None and model.get("snapshot"):
        with suppress(FileNotFoundError):
            snapshot = artifact(store, "snapshots", model["snapshot"])
            classes = read_file(store, workspace, snapshot / "manifest.json").get(
                "classes"
            )
    if not supports_clips(classes):
        raise ValueError(MODEL_ERROR)
    recipe = {
        "match_id": recording.source_id,
        "model": payload["model"],
        "options": asdict(options),
    }
    return schedule(workspace, actor, "clip", recipe, uuid.UUID(payload["request_id"]))


def listing(store: Store, workspace: Workspace) -> dict:
    """List recent attempts and recording metadata without loading frame labels."""
    runs = []
    for job in AnalysisJob.objects.filter(workspace=workspace, kind="clip").order_by(
        "-created_at"
    )[:50]:
        row = {
            "id": str(job.pk),
            "status": job.status,
            "recipe": job.payload,
            "created_at": job.created_at.isoformat(),
            "message": job.message,
            "frames": 0,
        }
        with suppress(FileNotFoundError):
            row.update(
                read_file(store, workspace, directory(store, str(job.pk)) / "run.json")
            )
        # Worker receipts bind the checkpoint hash; the durable recipe also names
        # the selected training run, needed when reopening or retrying a clip.
        row["recipe"] = {**job.payload, **row["recipe"]}
        # A hard-killed subprocess can leave a running receipt; the durable job wins.
        if job.status == "failed":
            row.update(status="failed", message=job.message)
        runs.append(row)
    recordings = [
        {
            "id": r.source_id,
            "title": r.metadata.get("title", r.source_id),
            "video": r.metadata.get("video"),
            "duration_seconds": r.metadata.get("duration_seconds", 0),
        }
        for r in Recording.objects.filter(workspace=workspace)
    ]
    return {"runs": runs, "recordings": recordings}


def result(store: Store, workspace: Workspace, run_id: str, chunk: str | None) -> dict:
    """Authorize the job before resolving an allowlisted result chunk.

    Raises:
        FileNotFoundError: The request or input does not satisfy this operation.

    """
    if not AnalysisJob.objects.filter(
        workspace=workspace, pk=uuid.UUID(run_id), kind="clip"
    ).exists():
        raise FileNotFoundError("Clip not found")
    root = directory(store, run_id)
    record = read_file(store, workspace, root / "run.json")
    if chunk is None:
        return record
    if not any(c["name"] == chunk for c in record.get("chunks", [])):
        raise FileNotFoundError("Chunk not found")
    return read_file(store, workspace, root / chunk)


def cancel(store: Store, workspace: Workspace, run_id: str) -> None:
    """Request a cooperative stop; never terminate another workspace's job.

    Raises:
        FileNotFoundError: The request or input does not satisfy this operation.

    """
    job = AnalysisJob.objects.filter(
        workspace=workspace, pk=uuid.UUID(run_id), kind="clip"
    ).first()
    if job is None:
        raise FileNotFoundError("Clip not found")
    if job.status in {"queued", "running"}:
        path = directory(store, str(job.pk)) / "cancel.json"
        atomic_json(path, {"requested": True})
        store.publish_artifact(path.relative_to(store.root).as_posix())
        AnalysisJob.objects.filter(pk=job.pk, status="queued").update(
            status="cancelled", message="Stopped before analysis started"
        )
