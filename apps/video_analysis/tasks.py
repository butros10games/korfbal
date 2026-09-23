"""Dedicated background entry point; web processes never load detector runtimes."""

import json
import logging

from celery import shared_task
from django.utils import timezone

from apps.video_analysis.composition import (
    queue_training,
    run_clip,
    run_detector,
    sync_workspace_files,
    worker_store,
)
from apps.video_analysis.engine import vision
from apps.video_analysis.engine.clips import receipt
from apps.video_analysis.engine.coverage import dataset_report
from apps.video_analysis.engine.handoff import parent_checkpoint
from apps.video_analysis.engine.luna import analyze_frame
from apps.video_analysis.engine.media import sample_frame
from apps.video_analysis.engine.store import Store, number
from apps.video_analysis.models import AnalysisJob, Workspace


@shared_task
def execute(job_id: str) -> None:
    """Execute a persisted request under the shared job system's exclusive lease."""
    job = AnalysisJob.objects.select_related("workspace", "requested_by").get(pk=job_id)
    if job.status in {"completed", "failed", "cancelled", "interrupted"}:
        return
    if not AnalysisJob.objects.filter(
        pk=job.pk, status__in=["queued", "running"]
    ).update(status="running"):
        return
    job.status = "running"
    store = worker_store(job.workspace, job.requested_by, hydrate=job.kind != "clip")
    try:
        perform(job, store)
        if job.kind not in {"train", "clip"}:
            store.sync_artifacts()
        job.status, job.message = (
            "completed",
            "GPU training queued. Follow the Cloud GPU run for training and cleanup."
            if job.kind == "train"
            else "Clip results are available in Clip runs."
            if job.kind == "clip"
            else "Ready. Reload the review queue or training inventory.",
        )
        if job.kind == "clip":
            result = receipt(store, str(job.pk))
            job.status, job.message = result["status"], result["message"]
    except Exception:
        logging.getLogger(__name__).exception("Video analysis job %s failed", job.pk)
        job.status, job.message = (
            "failed",
            "Analysis failed. An operator can inspect the worker logs.",
        )
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "message", "finished_at"])


def perform(job: AnalysisJob, store: Store) -> None:
    """Execute one validated operation.

    Raises:
        ValueError: The operation or selected model is invalid.

    """
    payload = job.payload
    if job.kind == "analyze":
        analyze_frame(store, payload["match_id"], payload["frame_id"], "codex")
    elif job.kind in {"sample", "sequence"}:
        seconds = number(payload.get("seconds"), 0, 86400)
        offsets = range(-4, 5) if job.kind == "sequence" else [0]
        for offset in offsets:
            sample_frame(store, payload["match_id"], max(0, seconds + offset * 0.08))
    elif job.kind == "freeze":
        vision.freeze(
            store,
            payload["name"],
            payload.get("profile", "people"),
            payload.get("selection", "all"),
        )
    elif job.kind == "train":
        dataset_report(store, payload["snapshot"])
        if payload.get("parent_run"):
            parent_checkpoint(store, payload["parent_run"])
        queue_training(store, str(job.pk), payload)
    elif job.kind == "propose":
        run_detector(store, payload["match_id"], proposal_weights(store, payload))
    elif job.kind == "clip":
        run_clip(store, str(job.pk), payload)
    else:
        raise ValueError("Unknown analysis job")


def proposal_weights(store: Store, payload: dict) -> str:
    """Resolve a completed checkpoint for the existing frame-proposal workflow.

    Raises:
        ValueError: The selected model is not a completed training run.

    """
    selected = payload.get("model", "pretrained")
    if selected == "pretrained":
        return "yolo26n.pt"
    root = vision.artifact(store, "runs", selected)
    record = json.loads((root / "run.json").read_text())
    if record["kind"] != "train" or record["status"] != "completed":
        raise ValueError("Select a completed training run")
    return str(root / "fit/weights/best.pt")


@shared_task
def publish_reviews(workspace_id: str) -> None:
    """Publish committed labels; durable generations recover concurrent edits."""
    workspace = Workspace.objects.get(pk=workspace_id)
    store = worker_store(workspace, None, hydrate=False)
    if store.files:
        store.files.publish_review(store.read())


@shared_task
def sync_files() -> None:
    """Recover private artifacts produced by the separate training controller."""
    for workspace in Workspace.objects.all():
        sync_workspace_files(workspace)
