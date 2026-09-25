"""One bounded preparation unit per durable dispatch, with fair continuation."""

from contextlib import suppress
from dataclasses import asdict
import logging
import uuid

from django.db import transaction
from django.utils import timezone

from apps.video_analysis.composition import (
    extract_pipeline_frames,
    import_pipeline_source,
    infer_pipeline_batch,
    pipeline_has_capacity,
    run_clip,
    worker_store,
)
from apps.video_analysis.engine.clip_contract import ClipOptions
from apps.video_analysis.engine.clips import directory, receipt
from apps.video_analysis.engine.store import Store, frame_version
from apps.video_analysis.models import (
    AnalysisJob,
    ClipReview,
    Frame,
    Recording,
    ReviewPipeline,
    VideoUpload,
    Workspace,
)
from apps.video_analysis.queries import frame_payload
from apps.video_analysis.services.clips import read_file
from apps.video_analysis.services.pipeline import ACTIVE, BATCH_SIZE, wake
from apps.video_analysis.services.pipeline_corrections import clip_drafts
from apps.video_analysis.services.review import publish_later
from apps.video_analysis.services.uploads import cleanup_later


@transaction.atomic
def claim(workspace_id: str) -> ReviewPipeline | None:
    """Share capacity with manual analysis; the durable job owns execution exclusion."""
    workspace = Workspace.objects.select_for_update().get(pk=workspace_id)
    if (
        AnalysisJob.objects
        .filter(workspace=workspace, status__in=ACTIVE)
        .exclude(kind="clip", payload__pipeline_id__isnull=False)
        .exists()
    ):
        wake(workspace.pk, 15)
        return None
    # A recovered running unit must finish before another queued pipeline starts.
    run = ReviewPipeline.objects.filter(workspace=workspace, status="running").first()
    if run is None:
        run = (
            ReviewPipeline.objects
            .filter(workspace=workspace, status="queued")
            .order_by("updated_at", "created_at")
            .first()
        )
    if run:
        run.status, run.message = "running", "Preparing the next bounded unit"
        run.revision += 1
        run.save()
    return run


def advance(workspace_id: str) -> None:
    """Retry worker death safely and yield between units so review stays interactive."""
    run = claim(workspace_id)
    if run is None:
        return
    try:
        store = worker_store(run.workspace, run.requested_by, hydrate=False)
        if not pipeline_has_capacity(store, importing=run.recipe["stage"] == "import"):
            wait_for_capacity(run)
            return
        progress, done = perform(run, store)
    except Exception:
        logging.getLogger(__name__).exception("Review pipeline %s failed", run.pk)
        with transaction.atomic():
            current = ReviewPipeline.objects.select_for_update().get(pk=run.pk)
            current.status = "failed"
            current.message = (
                "Preparation failed. Completed work is retained; "
                "inspect worker logs and retry."
            )
            current.revision += 1
            current.save()
            wake(workspace_id)
        return
    with transaction.atomic():
        current = ReviewPipeline.objects.select_for_update().get(pk=run.pk)
        pause = current.progress.get("pause_requested", False)
        current.progress = {**progress, "pause_requested": pause}
        current.status = (
            ("awaiting_cuts" if run.recipe["stage"] == "import" else "ready")
            if done
            else "paused"
            if pause
            else "queued"
        )
        current.message = (
            "Recording ready: mark the active match sections"
            if current.status == "awaiting_cuts"
            else "Drafts ready for human review"
            if done
            else "Waiting for the next processing slot"
        )
        current.revision += 1
        current.save()
        if current.status == "awaiting_cuts":
            upload = VideoUpload.objects.filter(pk=current.pk).first()
            if upload:
                cleanup_later(upload, timezone.now())
        wake(workspace_id)


def perform(run: ReviewPipeline, store: Store) -> tuple[dict, bool]:
    """Publish frame drafts first, then short tracking clips.

    Raises:
        ValueError: The source recording or cuts changed.

    """
    recipe, progress = run.recipe, dict(run.progress)
    if recipe["stage"] == "import":
        import_pipeline_source(store, recipe)
        return progress, True
    recording = Recording.objects.get(
        workspace=run.workspace, source_id=recipe["match_id"]
    )
    if (
        recording.metadata.get("active_periods") != recipe["periods"]
        or recording.metadata.get("video_sha256") != recipe["video_sha256"]
    ):
        raise ValueError("Source or cuts changed; create a new preparation")
    times = recipe["plan"]["times"]
    offset = progress.get("frames_done", 0)
    if offset < len(times):
        selected = times[offset : offset + BATCH_SIZE]
        ids = prepare_frames(run, store, recording, selected)
        candidates = []
        for frame in Frame.objects.filter(recording=recording, source_id__in=ids):
            if (
                frame.status == "pending"
                and frame.correction is None
                and frame.proposal is None
                and frame.metadata.get("dataset_decision") != "removed"
            ):
                candidates.append({
                    "id": frame.source_id,
                    "image": frame.metadata["image"],
                    "frame_version": frame_version(frame_payload(frame)),
                })
        if candidates:
            output = f"vision/pipeline/{run.pk}/batch-{offset:06d}.json"
            drafts = clip_drafts(run, store, candidates)
            seeded = {row["id"] for row in drafts}
            missing = [row for row in candidates if row["id"] not in seeded]
            if missing:
                drafts.extend(
                    infer_pipeline_batch(store, recipe["model"], missing, output)[
                        "frames"
                    ]
                )
            publish_proposals(run, {"frames": drafts}, output)
        progress["frames_done"] = offset + len(selected)
        return progress, False
    clips = recipe["plan"]["clips"]
    index = progress.get("clips_done", 0)
    if index < len(clips):
        prepare_clip(run, store, recording, clips[index], index)
        progress["clips_done"] = index + 1
    return progress, progress.get("clips_done", 0) == len(clips)


@transaction.atomic
def publish_proposals(run: ReviewPipeline, result: dict, output: str) -> None:
    """Never replace human work or publish a draft against a changed frame.

    Raises:
        ValueError: The source or cuts changed during inference.

    """
    workspace = Workspace.objects.select_for_update().get(pk=run.workspace_id)
    recording = Recording.objects.get(
        workspace=workspace, source_id=run.recipe["match_id"]
    )
    if (
        recording.metadata.get("active_periods") != run.recipe["periods"]
        or recording.metadata.get("video_sha256") != run.recipe["video_sha256"]
    ):
        raise ValueError("Recording or cuts changed during inference")
    changed = False
    for item in result["frames"]:
        frame = Frame.objects.filter(
            recording__workspace=workspace,
            recording__source_id=run.recipe["match_id"],
            source_id=item["id"],
        ).first()
        if frame is None:
            continue
        if (
            frame.proposal is not None
            or frame.correction is not None
            or frame.status != "pending"
            or frame.metadata.get("dataset_decision") == "removed"
            or frame_version(frame_payload(frame)) != item["frame_version"]
        ):
            continue
        frame.proposal = item["prediction"]
        frame.metadata = {
            **frame.metadata,
            "model": run.recipe["model"],
            "proposal_artifact": (
                f"vision/clips/{item['source_clip']}/run.json"
                if item.get("source_clip")
                else output
            ),
        }
        frame.save(update_fields=["proposal", "metadata"])
        changed = True
    if changed:
        workspace.revision += 1
        workspace.save(update_fields=["revision"])
        publish_later(workspace)


def prepare_clip(
    run: ReviewPipeline, store: Store, recording: Recording, section: dict, index: int
) -> None:
    """Publish one replay and make it available for explicit inspection.

    Raises:
        ValueError: The replay did not finish.

    """
    prior = (
        AnalysisJob.objects
        .filter(
            workspace=run.workspace,
            payload__pipeline_id=str(run.pk),
            payload__pipeline_unit=index,
        )
        .order_by("-created_at")
        .first()
    )
    if prior and prior.status == "completed":
        ClipReview.objects.get_or_create(job=prior, defaults={"pipeline": run})
        return
    if prior:
        with suppress(FileNotFoundError):
            previous = read_file(
                store, run.workspace, directory(store, str(prior.pk)) / "run.json"
            )
            if previous.get("status") == "completed":
                prior.status, prior.finished_at = "completed", timezone.now()
                prior.save(update_fields=["status", "finished_at"])
                ClipReview.objects.get_or_create(job=prior, defaults={"pipeline": run})
                return
        AnalysisJob.objects.filter(pk=prior.pk, status="running").update(
            status="interrupted"
        )
    attempt = AnalysisJob.objects.filter(
        workspace=run.workspace,
        payload__pipeline_id=str(run.pk),
        payload__pipeline_unit=index,
    ).count()
    run_id = uuid.uuid5(run.pk, f"clip-{index}-attempt-{attempt}")
    payload = {
        "match_id": recording.source_id,
        "model": run.recipe["model"],
        "pipeline_id": str(run.pk),
        "pipeline_unit": index,
        "options": asdict(
            ClipOptions.parse({
                **section,
                "court": {"mode": "automatic", "length": 40, "width": 20},
            })
        ),
    }
    job, _ = AnalysisJob.objects.get_or_create(
        pk=run_id,
        defaults={
            "workspace": run.workspace,
            "requested_by": run.requested_by,
            "kind": "clip",
            "payload": payload,
            "status": "running",
        },
    )
    if job.status != "completed":
        AnalysisJob.objects.filter(pk=job.pk).update(status="running")
        try:
            run_clip(store, str(run_id), payload)
            result = receipt(store, str(run_id))
            if result["status"] != "completed":
                raise ValueError("Tracking clip did not finish")
        except Exception:
            AnalysisJob.objects.filter(pk=job.pk).update(
                status="failed", finished_at=timezone.now()
            )
            raise
        job.status, job.finished_at = "completed", timezone.now()
        job.save(update_fields=["status", "finished_at"])
    ClipReview.objects.get_or_create(job=job, defaults={"pipeline": run})


def prepare_frames(
    run: ReviewPipeline, store: Store, recording: Recording, times: list[float]
) -> list[str]:
    """Prepare only missing selected images, independently of catalogue size."""
    ids = [f"at-{round(time * 1000):09d}" for time in times]
    existing = set(
        Frame.objects.filter(recording=recording, source_id__in=ids).values_list(
            "source_id", flat=True
        )
    )
    missing = [
        time
        for time, frame_id in zip(times, ids, strict=True)
        if frame_id not in existing
    ]
    if missing:
        rows = extract_pipeline_frames(store, recording.metadata, missing)
        save_frames(run, recording, rows)
    return ids


@transaction.atomic
def save_frames(run: ReviewPipeline, recording: Recording, rows: list[dict]) -> None:
    """Publish extracted frames only against the same immutable input selection.

    Raises:
        ValueError: The cuts changed during extraction.

    """
    workspace = Workspace.objects.select_for_update().get(pk=run.workspace_id)
    recording.refresh_from_db()
    if (
        recording.metadata.get("active_periods") != run.recipe["periods"]
        or recording.metadata.get("video_sha256") != run.recipe["video_sha256"]
    ):
        raise ValueError("Recording or cuts changed during preparation")
    for row in rows:
        Frame.objects.get_or_create(
            recording=recording,
            source_id=row["id"],
            defaults={
                "metadata": {k: v for k, v in row.items() if k != "id"},
                "position": round(row["time_seconds"] * 1000),
            },
        )
    workspace.revision += 1
    workspace.save(update_fields=["revision"])
    publish_later(workspace)


@transaction.atomic
def wait_for_capacity(run: ReviewPipeline) -> None:
    """Yield without consuming progress when staging space is temporarily full."""
    current = ReviewPipeline.objects.select_for_update().get(pk=run.pk)
    current.status = "paused" if current.progress.get("pause_requested") else "queued"
    current.message = "Waiting for free worker staging space"
    current.revision += 1
    current.save()
    wake(current.workspace_id, 60)
