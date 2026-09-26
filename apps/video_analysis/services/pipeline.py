"""Durable intake, immutable preparation plans, and explicitly human clip review."""

from datetime import timedelta
import math
from urllib.parse import urlsplit
import uuid

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.kwt_common.models import BackgroundJob
from apps.kwt_common.services.jobs import enqueue
from apps.video_analysis.engine.clip_contract import finite
from apps.video_analysis.engine.clip_models import supports_clips
from apps.video_analysis.engine.store import ConflictError, Store
from apps.video_analysis.engine.timeline import sample_times, validate_periods
from apps.video_analysis.engine.vision import artifact
from apps.video_analysis.models import (
    ClipReview,
    Recording,
    ReviewPipeline,
    VideoUpload,
    Workspace,
)
from apps.video_analysis.services.clips import read_file
from apps.video_analysis.services.review import timeline


BATCH_SIZE = 25
CLIP_SECONDS = 30
PAGE_SIZE = 24
RUN_PAGE_SIZE = 50
ACTIVE = ("queued", "running")
MAX_PIPELINES = 100
MAX_CLIPS = 480
MAX_TEXT = 2000


def wake(workspace_id: object, delay: int = 0) -> None:
    """Persist recovery/continuation under one exclusive workspace execution key."""
    enqueue(
        "apps.video_analysis.tasks.advance_pipeline",
        str(workspace_id),
        args=[str(workspace_id)],
        queue="vision",
        due_at=timezone.now() + timedelta(seconds=delay),
    )


def plan(periods: list[dict], interval: int) -> dict:
    """Split play without crossing breaks or creating subsecond tails.

    Raises:
        ValueError: Selected duration exceeds the preparation policy.

    """
    times = sample_times(periods, interval)
    clips = []
    for period in periods:
        duration = period["end"] - period["start"]
        if duration < 1:
            raise ValueError("Each play section must last at least one second")
        count = math.ceil(duration / CLIP_SECONDS)
        for index in range(count):
            start = round(period["start"] + index * duration / count, 3)
            end = round(period["start"] + (index + 1) * duration / count, 3)
            clips.append({"start": start, "duration": round(end - start, 3)})
    if len(clips) > MAX_CLIPS:
        raise ValueError("Select at most four hours of active play")
    return {"times": times, "clips": clips, "version": 1}


def validated_model(store: Store, workspace: Workspace, model: str) -> str:
    """Pin a completed detector supporting all four required classes.

    Raises:
        ValueError: The selected model is incompatible.

    """
    root = artifact(store, "runs", model)
    record = read_file(store, workspace, root / "run.json")
    classes = record.get("classes")
    if classes is None and record.get("snapshot"):
        classes = read_file(
            store,
            workspace,
            artifact(store, "snapshots", record["snapshot"]) / "manifest.json",
        ).get("classes")
    if (
        record.get("kind") != "train"
        or record.get("status") != "completed"
        or not supports_clips(classes)
    ):
        raise ValueError(
            "Choose a completed model supporting players, referees, ball and basket"
        )
    return model


@transaction.atomic
def submit(
    workspace: Workspace, actor: User, store: Store, payload: dict
) -> ReviewPipeline:
    """Accept idempotent intake or preparation, allowing a durable backlog.

    Raises:
        ValueError: The queue is full.
        ConflictError: The request ID belongs to another operation.

    """
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    request_id = uuid.UUID(payload["request_id"])
    intent = {
        k: payload.get(k)
        for k in (
            "source_url",
            "match_id",
            "model",
            "interval",
            "active_periods",
            "clip_review_id",
            "seconds",
        )
    }
    existing = ReviewPipeline.objects.filter(pk=request_id).first()
    if existing:
        if (
            existing.workspace_id != workspace.pk
            or existing.requested_by_id != actor.pk
            or existing.recipe.get("intent") != intent
        ):
            raise ConflictError("Request ID already used for another preparation")
        return existing
    if VideoUpload.objects.filter(pk=request_id).exists():
        raise ConflictError("Request ID already belongs to an upload")
    if (
        ReviewPipeline.objects.filter(workspace=workspace, status__in=ACTIVE).count()
        >= MAX_PIPELINES
    ):
        raise ValueError("The intake queue is full; wait for existing work to finish")
    recipe = {"intent": intent, "version": 1}
    recipe.update(build_recipe(workspace, store, payload, request_id, intent))
    run = ReviewPipeline.objects.create(
        id=request_id, workspace=workspace, requested_by=actor, recipe=recipe
    )
    if recipe["stage"] == "frames":
        ReviewPipeline.objects.filter(
            workspace=workspace,
            recipe__match_id=recipe["match_id"],
            status="awaiting_cuts",
        ).update(
            status="prepared", revision=F("revision") + 1, updated_at=timezone.now()
        )
    wake(workspace.pk)
    return run


@transaction.atomic
def control(workspace: Workspace, payload: dict) -> None:
    """Pause, resume or retry without repeating finished units.

    Raises:
        ValueError: The action is invalid for this state.
        ConflictError: The pipeline revision changed.

    """
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    run = ReviewPipeline.objects.select_for_update().get(
        workspace=workspace, pk=uuid.UUID(payload["id"])
    )
    if payload.get("revision") != run.revision:
        raise ConflictError("Preparation changed; refresh before trying again")
    action = payload.get("action")
    if action == "pause" and run.status in ACTIVE:
        # Running work finishes its bounded unit, then honors this request.
        run.progress = {**run.progress, "pause_requested": True}
        if run.status == "queued":
            run.status = "paused"
    elif action == "resume" and (
        run.status in {"paused", "failed"}
        or (run.status == "running" and stalled(workspace))
    ):
        run.status = "queued"
        run.progress = {**run.progress, "pause_requested": False}
        run.message = "Queued to resume from the last completed unit"
    else:
        raise ValueError("This preparation cannot perform that action")
    run.revision += 1
    run.save()
    wake(workspace.pk)


def listing(workspace: Workspace, after: int = 0, before: str = "") -> dict:
    """Bound intake history and the independently pageable pending clip queue."""
    worker_stalled = stalled(workspace)
    runs = ReviewPipeline.objects.filter(workspace=workspace)
    if before:
        cursor = runs.get(pk=uuid.UUID(before))
        runs = runs.filter(
            Q(created_at__lt=cursor.created_at)
            | Q(created_at=cursor.created_at, pk__lt=cursor.pk)
        )
    page = list(runs.order_by("-created_at", "-pk")[: RUN_PAGE_SIZE + 1])
    reviews = list(
        ClipReview.objects
        .filter(pipeline__workspace=workspace)
        .exclude(status="approved")
        .filter(pk__gt=after)
        .select_related("job")
        .order_by("pk")[: PAGE_SIZE + 1]
    )
    return {
        "runs": [
            {
                "id": str(r.pk),
                "status": "failed"
                if r.status == "running" and worker_stalled
                else r.status,
                "message": r.message,
                "revision": r.revision,
                "match_id": r.recipe["match_id"],
                "source_url": r.recipe.get("source_url", ""),
                "source_name": r.recipe.get("upload", {}).get("name", ""),
                "model": r.recipe.get("model", ""),
                "stage": r.recipe["stage"],
                "progress": r.progress,
                "frames_total": len(r.recipe.get("plan", {}).get("times", [])),
                "clips_total": len(r.recipe.get("plan", {}).get("clips", [])),
                "first_frame_id": (
                    f"at-{round(r.recipe['plan']['times'][0] * 1000):09d}"
                    if r.recipe.get("plan", {}).get("times")
                    else ""
                ),
            }
            for r in page[:RUN_PAGE_SIZE]
        ],
        "next_runs": str(page[RUN_PAGE_SIZE - 1].pk)
        if len(page) > RUN_PAGE_SIZE
        else None,
        "clips": [
            {
                "id": r.pk,
                "run_id": str(r.job_id),
                "match_id": r.job.payload["match_id"],
                "start": r.job.payload["options"]["start"],
                "duration": r.job.payload["options"]["duration"],
                "status": r.status,
                "notes": r.notes,
                "revision": r.revision,
            }
            for r in reviews[:PAGE_SIZE]
        ],
        "next": reviews[PAGE_SIZE - 1].pk if len(reviews) > PAGE_SIZE else None,
    }


@transaction.atomic
def review_clip(workspace: Workspace, actor: User, payload: dict) -> None:
    """Save an inspection verdict without approving frame labels.

    Raises:
        ValueError: The review is incomplete or invalid.
        ConflictError: Another reviewer changed this verdict.

    """
    row = ClipReview.objects.select_for_update().get(
        pk=payload["id"], pipeline__workspace=workspace
    )
    if payload.get("revision") != row.revision:
        raise ConflictError("Another reviewer changed this clip")
    status, notes = payload.get("status"), payload.get("notes", "")
    if (
        status not in {"approved", "needs_work", "pending"}
        or not isinstance(notes, str)
        or len(notes) > MAX_TEXT
    ):
        raise ValueError("Invalid clip review")
    if status == "approved" and payload.get("watched_complete") is not True:
        raise ValueError("Confirm that the entire clip was inspected")
    if status == "needs_work" and not notes.strip():
        raise ValueError("Describe the timestamp and tracking issue")
    row.revision += 1
    row.status, row.notes = status, notes
    row.history = [
        *row.history,
        {
            "actor": actor.pk,
            "at": timezone.now().isoformat(),
            "status": status,
            "notes": notes,
            "revision": row.revision,
        },
    ]
    row.save()


def clip_detail(workspace: Workspace, review_id: int) -> dict:
    """Authorize direct links independently of the queue's pagination."""
    row = ClipReview.objects.select_related("job").get(
        pk=review_id, pipeline__workspace=workspace
    )
    return {
        "id": row.pk,
        "run_id": str(row.job_id),
        "match_id": row.job.payload["match_id"],
        "start": row.job.payload["options"]["start"],
        "duration": row.job.payload["options"]["duration"],
        "status": row.status,
        "notes": row.notes,
        "revision": row.revision,
    }


def build_recipe(
    workspace: Workspace,
    store: Store,
    payload: dict,
    request_id: uuid.UUID,
    intent: dict,
) -> dict:
    """Freeze source, cuts and model before admitting worker work.

    Raises:
        ValueError: Inputs are unsupported.
        ConflictError: The recording already has queued preparation.

    """
    recipe = {}
    if payload.get("source_url"):
        url = payload["source_url"]
        if not isinstance(url, str) or len(url) > MAX_TEXT:
            raise ValueError("Use a public Eyecons recording link")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc not in {"eyecons.com", "www.eyecons.com"}
            or not parsed.path.startswith("/videos/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use an https://eyecons.com/videos/... recording link")
        if (
            ReviewPipeline.objects
            .filter(workspace=workspace, recipe__source_url=url)
            .exclude(status="cancelled")
            .exists()
        ):
            raise ConflictError(
                "This recording is already imported or in the intake queue"
            )
        recipe.update(
            source_url=url, match_id=f"intake-{request_id.hex}", stage="import"
        )
    elif payload.get("clip_review_id"):
        clip = ClipReview.objects.select_related("job", "pipeline").get(
            pk=payload["clip_review_id"], pipeline__workspace=workspace
        )
        options = clip.job.payload["options"]
        time = finite(
            payload.get("seconds"),
            options["start"],
            options["start"] + options["duration"] - 0.001,
        )
        step = 1 / options.get("fps", 12.5)
        time = options["start"] + round((time - options["start"]) / step) * step
        times = [
            round(time + offset * step, 3)
            for offset in range(-4, 5)
            if options["start"]
            <= time + offset * step
            < options["start"] + options["duration"]
        ]
        recipe.update(clip.pipeline.recipe)
        recipe.update(
            intent=intent,
            stage="correction",
            clip_run_id=str(clip.job_id),
            plan={"times": times, "clips": [], "version": 1},
        )
    else:
        if "active_periods" in payload:
            timeline(workspace, payload)
        recording = Recording.objects.filter(
            workspace=workspace, source_id=payload.get("match_id")
        ).first()
        if recording is None or not recording.metadata.get("video"):
            raise ValueError("Choose an imported recording with video")
        if ReviewPipeline.objects.filter(
            workspace=workspace, recipe__match_id=recording.source_id, status__in=ACTIVE
        ).exists():
            raise ConflictError("This recording already has a preparation in progress")
        periods = validate_periods(
            recording.metadata.get("active_periods"),
            recording.metadata["duration_seconds"],
        )
        interval = payload.get("interval", 20)
        recipe.update(
            match_id=recording.source_id,
            stage="frames",
            periods=periods,
            video_sha256=recording.metadata.get("video_sha256"),
            model=validated_model(store, workspace, payload["model"]),
            interval=interval,
            plan=plan(periods, interval),
        )
    return recipe


def stalled(workspace: Workspace) -> bool:
    """Expose the durable envelope's exhausted retries as an actionable failure."""
    return (
        BackgroundJob.objects
        .filter(
            key=f"apps.video_analysis.tasks.advance_pipeline:{workspace.pk}",
            due_at__isnull=True,
        )
        .exclude(error="")
        .exists()
    )
