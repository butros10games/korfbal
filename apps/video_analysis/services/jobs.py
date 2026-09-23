"""Persist user-requested analysis work before publishing to a dedicated worker."""

from typing import Any
import uuid

from django.contrib.auth.models import User
from django.db import transaction

from apps.kwt_common.services.jobs import enqueue
from apps.video_analysis.engine.store import ConflictError
from apps.video_analysis.models import AnalysisJob, Workspace


@transaction.atomic
def schedule(
    workspace: Workspace,
    actor: User,
    kind: str,
    payload: dict[str, Any],
    request_id: uuid.UUID | None = None,
) -> AnalysisJob:
    """Allow one active preparation job per workspace.

    Raises:
        ConflictError: Another request is still pending or running.

    """
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    if request_id:
        existing = AnalysisJob.objects.filter(pk=request_id).first()
        if existing:
            if (
                existing.workspace_id != workspace.pk
                or existing.requested_by_id != actor.pk
                or existing.kind != kind
                or existing.payload != payload
            ):
                raise ConflictError("Request ID already used")
            return existing
    if AnalysisJob.objects.filter(
        workspace=workspace, status__in=["queued", "running"]
    ).exists():
        raise ConflictError("An analysis job is already queued or running")
    job = AnalysisJob.objects.create(
        id=request_id or uuid.uuid4(),
        workspace=workspace,
        requested_by=actor,
        kind=kind,
        payload=payload,
    )
    enqueue(
        "apps.video_analysis.tasks.execute",
        str(job.pk),
        args=[str(job.pk)],
        queue="vision",
        once=True,
    )
    return job


@transaction.atomic
def continue_analysis(job_id: str) -> None:
    """Persist the next replay section on the same exclusive durable job key."""
    job = AnalysisJob.objects.select_for_update().get(pk=job_id)
    if job.status != "running":
        return
    job.status, job.message = (
        "queued",
        "Next replay section queued; completed frames are ready to watch.",
    )
    job.save(update_fields=["status", "message"])
    enqueue(
        "apps.video_analysis.tasks.execute",
        str(job.pk),
        args=[str(job.pk)],
        queue="vision",
    )
