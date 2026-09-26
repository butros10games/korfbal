"""Session/MFA-protected JSON and private range-capable media adapters."""

from collections.abc import Callable
from functools import wraps
from http import HTTPStatus
import io
import json
import mimetypes
from typing import Any, cast
import uuid
import zipfile

from django.contrib.auth.models import User
from django.core.handlers.asgi import ASGIRequest
from django.db import transaction
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponseBase,
    HttpResponseRedirect,
    JsonResponse,
    StreamingHttpResponse,
)
from django.middleware.csrf import get_token
from django.utils.crypto import constant_time_compare

from apps.video_analysis.api.clip_views import clip_endpoint
from apps.video_analysis.api.pipeline_views import pipeline_endpoint
from apps.video_analysis.api.streaming import async_chunks
from apps.video_analysis.api.upload_views import upload_endpoint
from apps.video_analysis.composition import (
    accepted_policy,
    cancel_training,
    intake_store,
    launch_status,
    review_store,
    snapshot_download,
)
from apps.video_analysis.engine import monitor, vision
from apps.video_analysis.engine.server import parse_range
from apps.video_analysis.engine.store import ConflictError, Store, frame_version
from apps.video_analysis.engine.timeline import sample_times, validate_periods
from apps.video_analysis.engine.training import PRETRAINED_WEIGHTS
from apps.video_analysis.models import (
    AnalysisJob,
    ClipReview,
    Recording,
    ReviewPipeline,
    VideoUpload,
    Workspace,
)
from apps.video_analysis.queries import (
    blind_queue,
    check_queue,
    registered_media,
    review_queue,
    review_state,
)
from apps.video_analysis.services import curation, dataset, review
from apps.video_analysis.services.jobs import schedule


MAX_BODY = 100_000


def secured(view: Callable[..., HttpResponseBase]) -> Callable[..., HttpResponseBase]:
    """Require the same password-bound MFA proof as Korfbal administration."""

    @wraps(view)
    def wrapped(request: HttpRequest, action: str) -> HttpResponseBase:
        user = cast(User, request.user)
        verified = request.session.get("bg_auth_mfa_verified", "")
        if not user.is_authenticated:
            response = JsonResponse(
                {"error": "Sign in with MFA to review footage."}, status=401
            )
        elif not (
            user.is_active
            and user.is_staff
            and isinstance(verified, str)
            and constant_time_compare(verified, user.get_session_auth_hash())
        ):
            response = JsonResponse(
                {"error": "Staff access with verified MFA is required."}, status=403
            )
        else:
            try:
                response = view(request, action)
            except Workspace.DoesNotExist:
                response = (
                    JsonResponse({
                        "runs": [],
                        "clips": [],
                        "next": None,
                        "csrf": get_token(request),
                    })
                    if action == "pipeline" and request.method == "GET"
                    else JsonResponse(
                        {"error": "The video workspace has not been imported yet."},
                        status=503,
                    )
                )
            except (
                FileNotFoundError,
                ReviewPipeline.DoesNotExist,
                ClipReview.DoesNotExist,
                VideoUpload.DoesNotExist,
            ):
                response = JsonResponse(
                    {"error": "Training artifact not found."}, status=404
                )
            except ConflictError as error:
                response = JsonResponse({"error": str(error)}, status=409)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                response = JsonResponse(
                    {
                        "error": str(error)
                        if action
                        in {
                            "curation",
                            "vision/freeze",
                            "vision/split",
                            "clips",
                            "timeline",
                            "prepare",
                            "pipeline",
                            "pipeline/control",
                            "pipeline/review",
                            "pipeline/upload",
                            "pipeline/upload/part",
                            "pipeline/upload/finish",
                            "pipeline/upload/cancel",
                        }
                        and isinstance(error, ValueError)
                        else "Invalid review request."
                    },
                    status=400,
                )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    return wrapped


@secured
def endpoint(request: HttpRequest, action: str) -> HttpResponseBase:
    """Keep the editor's tested wire contract under the native Django API.

    Raises:
        TypeError: The JSON input is not an object.

    """
    if request.method == "GET" and action == "access":
        return JsonResponse({"allowed": True})
    store, workspace = (
        intake_store(cast(User, request.user))
        if action in {"pipeline", "pipeline/upload"} and request.method == "POST"
        else review_store(cast(User, request.user), hydrate=False)
    )
    if action.startswith("pipeline") or action in {
        "clips",
        "clips/result",
        "clips/cancel",
        "clips/delete",
    }:
        handler = (
            upload_endpoint
            if action.startswith("pipeline/upload")
            else pipeline_endpoint
            if action.startswith("pipeline")
            else clip_endpoint
        )
        return handler(request, action, store, workspace)
    if store.files and action in {
        "vision",
        "monitor",
        "curation",
        "vision/split",
        "vision/train",
        "vision/cancel",
        "vision/freeze",
        "vision/prediction",
        "vision/queue",
        "vision/download",
    }:
        store.files.hydrate_artifacts(metadata_only=True)
    if request.method == "GET":
        return (
            media(request, store, workspace)
            if action == "media"
            else JsonResponse(
                check_queue(workspace)
                if request.GET.get("kind") == "check"
                else blind_queue(workspace)
                if request.GET.get("kind") == "blind"
                else review_queue(workspace)
            )
            if action == "queue"
            else read(request, action, store, workspace)
        )
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    if len(request.body) > MAX_BODY or request.content_type != "application/json":
        return JsonResponse({"error": "Expected a bounded JSON body"}, status=400)
    payload = json.loads(request.body)
    if not isinstance(payload, dict):
        raise TypeError("Expected object")
    return (
        JsonResponse(review.timeline(workspace, payload))
        if action == "timeline"
        else mutate(action, payload, store, workspace, request)
    )


def mutate(
    action: str,
    payload: dict[str, Any],
    store: Store,
    workspace: Workspace,
    request: HttpRequest,
) -> HttpResponseBase:
    """Apply short writes or persist background work."""
    if action in {"dataset", "review", "curation"}:
        if action == "curation":
            result = curation.save(workspace, store, cast(User, request.user), payload)
        elif action == "dataset":
            result = dataset.decide(workspace, cast(User, request.user), payload)
        else:
            result = review.save(workspace, cast(User, request.user), payload)
        return JsonResponse(result)
    if action == "vision/split":
        with store.transaction():
            vision.assign_split(
                store,
                payload["group"],
                payload["split"],
                override_frozen=payload.get("override_frozen") is True,
            )
            store.publish_artifact("vision/splits.json")
        return JsonResponse({"ok": True})
    if action == "vision/train":
        return start_training(payload, store, workspace, request)
    if action == "vision/cancel":
        cancel_training(store, payload["job_id"])
        return JsonResponse({"ok": True})
    if action == "vision/freeze" and payload.get("selection") == "curated":
        vision.select_frames(
            store.read(),
            vision.assignments(store),
            payload.get("profile", "people"),
            "curated",
        )
    kind = action.removeprefix("vision/")
    if kind == "prepare":
        payload = prepare_payload(workspace, payload)
    if kind in {"analyze", "sample", "sequence", "freeze", "propose", "prepare"}:
        job = schedule(workspace, cast(User, request.user), kind, payload)
        return JsonResponse(
            {"ok": True, "job_id": str(job.pk), "queued": True}, status=202
        )
    return JsonResponse({"error": "Unknown action"}, status=404)


def prepare_payload(workspace: Workspace, payload: dict[str, Any]) -> dict[str, Any]:
    """Bind preparation to a saved video and bounded active intervals.

    Raises:
        ValueError: If there is no usable video or selection.

    """
    record = (
        Recording.objects
        .filter(workspace=workspace, source_id=payload.get("match_id"))
        .values("source_id", "metadata")
        .first()
    )
    if record is None or not record["metadata"].get("video"):
        raise ValueError("Choose a recording with video")
    metadata = record["metadata"]
    periods = validate_periods(
        metadata.get("active_periods"), metadata["duration_seconds"]
    )
    sample_times(periods, payload.get("interval"))
    return {
        "match_id": record["source_id"],
        "interval": payload["interval"],
        "active_periods": periods,
    }


def training_selection(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize explicit model and price choices before retry comparison.

    Raises:
        ValueError: The model is unsupported or the price is explicitly empty.

    """
    weights = payload.get("base_weights", "yolo26n.pt")
    if not isinstance(weights, str) or weights not in PRETRAINED_WEIGHTS:
        raise ValueError("Choose a supported pretrained model")
    if payload.get("parent_run") and weights != "yolo26n.pt":
        raise ValueError("Choose a pretrained model or an existing checkpoint")
    result: dict[str, Any] = {"base_weights": weights}
    if "max_hourly_usd" in payload:
        if payload["max_hourly_usd"] is None:
            raise ValueError("Choose an hourly price limit")
        result["max_hourly_usd"] = payload["max_hourly_usd"]
    return result


@transaction.atomic
def start_training(
    payload: dict[str, Any], store: Store, workspace: Workspace, request: HttpRequest
) -> HttpResponseBase:
    """Accept an idempotent paid training request.

    Raises:
        ConflictError: A request ID was reused for different work.
        TypeError: The base model identifier is not a string.
        ValueError: The selected base model is not a completed training run.

    """
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    request_id = uuid.UUID(payload["request_id"])
    epochs = payload.get("epochs", 10)
    if type(epochs) is not int or epochs not in {5, 10, 30}:
        return JsonResponse({"error": "Choose 5, 10 or 30 epochs."}, status=400)
    recipe: dict[str, Any] = {"snapshot": payload["snapshot"], "epochs": epochs}
    recipe.update(training_selection(payload))
    parent_run = payload.get("parent_run", "")
    if not isinstance(parent_run, str):
        raise TypeError("Invalid base model")
    if parent_run:
        recipe["parent_run"] = parent_run
    previous = AnalysisJob.objects.filter(pk=request_id).first()
    if previous:
        defaults = {
            "parent_run": "",
            "base_weights": "yolo26n.pt",
            "max_hourly_usd": None,
        }
        prior = dict(defaults, **previous.payload)
        current = dict(defaults, **recipe)
        if (
            previous.workspace_id != workspace.pk
            or previous.requested_by_id != request.user.pk
            or previous.kind != "train"
            or any(prior.get(k) != v for k, v in current.items())
        ):
            raise ConflictError("Training request ID already used")
        return JsonResponse({"queued": True, "job_id": str(previous.pk)}, status=202)
    if parent_run:
        parent = json.loads(
            (vision.artifact(store, "runs", parent_run) / "run.json").read_text()
        )
        if parent.get("kind") != "train" or parent.get("status") != "completed":
            raise ValueError("Select a completed training run")
    recipe["policy"] = accepted_policy(
        store, payload["policy_version"], recipe.get("max_hourly_usd")
    )
    # Full image/label/checkpoint verification belongs to the queued preparation
    # job, before it can submit any paid work to the controller.
    json.loads(
        (
            vision.artifact(store, "snapshots", recipe["snapshot"]) / "manifest.json"
        ).read_text()
    )
    job = schedule(workspace, cast(User, request.user), "train", recipe, request_id)
    return JsonResponse({"queued": True, "job_id": str(job.pk)}, status=202)


def read(
    request: HttpRequest, action: str, store: Store, workspace: Workspace
) -> HttpResponseBase:
    """Read annotations and allowlisted monitoring; never expose worker payloads."""
    if action in {"curation", "dataset"}:
        result = (
            curation.listing(workspace, store, request.GET.dict())
            if action == "curation"
            else dataset.listing(
                workspace,
                request.GET.get("decision", "unreviewed"),
                int(request.GET.get("after", "0")),
                request.GET.get("recording", ""),
            )
        )
        return JsonResponse(dict(result, csrf=get_token(request)))
    if action == "state":
        if request.GET.get("scope") == "recording":
            data = review_state(workspace, request.GET.get("match", ""))
        else:
            data = store.read()
            for match in data["matches"]:
                for frame in match["frames"]:
                    frame["frame_version"] = frame_version(frame)
        return JsonResponse(
            dict(data, csrf=get_token(request), provider="codex", luna_available=True)
        )
    if action == "vision":
        status = launch_status(store)
        if AnalysisJob.objects.filter(
            workspace=workspace, status__in=["queued", "running"]
        ).exists():
            status.update(
                ready=False,
                reason="Wait for the current preparation or analysis job to finish.",
            )
        return JsonResponse(dict(vision.inventory(store), training=status))
    if action == "monitor":
        result = monitor.summary(store)
        result["jobs"].extend(
            {
                "id": str(j.pk),
                "source": "local",
                "kind": "prepare_training" if j.kind == "train" else j.kind,
                "status": j.status,
                "created_at": j.created_at.isoformat(),
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                "attention": j.status == "failed",
            }
            for j in AnalysisJob.objects.filter(workspace=workspace).order_by(
                "-created_at"
            )[:100]
        )
        return JsonResponse(result)
    if action == "job":
        job = (
            AnalysisJob.objects
            .filter(workspace=workspace)
            .order_by("-created_at")
            .first()
        )
        return JsonResponse({
            "running": bool(job and job.status in {"queued", "running"}),
            "message": job.message if job else "",
        })
    return artifact_response(request, action, store, workspace)


def artifact_response(
    request: HttpRequest, action: str, store: Store, workspace: Workspace
) -> HttpResponseBase:
    """Read model artifacts and authenticated downloads."""
    if action == "vision/prediction":
        return JsonResponse(
            vision.read_prediction(
                store,
                request.GET.get("run", ""),
                request.GET.get("match", ""),
                request.GET.get("frame", ""),
            )
        )
    if action == "vision/queue":
        return JsonResponse({
            "frames": vision.review_queue(store, request.GET.get("run", ""))
        })
    if action == "export":
        return FileResponse(
            io.BytesIO(store.export()),
            as_attachment=True,
            filename="reviewed-labels.zip",
        )
    if action == "vision/download":
        if getattr(store, "files", None):
            relative = snapshot_download(workspace, request.GET.get("id", ""))
            return stored_response(request, store, relative)
        root = vision.artifact(store, "snapshots", request.GET.get("id", ""))
        vision.verify_snapshot(root)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for member in root.rglob("*"):
                if member.is_file():
                    archive.write(member, str(member.relative_to(root.parent)))
        output.seek(0)
        return FileResponse(output, as_attachment=True, filename=f"{root.name}.zip")
    return JsonResponse({"error": "Unknown action"}, status=404)


def media(request: HttpRequest, store: Store, workspace: Workspace) -> HttpResponseBase:
    """Serve only registered workspace media after MFA authorization."""
    relative = request.GET.get("path", "")
    if not registered_media(workspace, relative):
        return JsonResponse({"error": "Unknown media"}, status=404)
    files = getattr(store, "files", None)
    if files and request.GET.get("delivery") == "direct":
        return stored_response(request, store, relative)
    return stream_response(request, store, relative)


def stored_response(
    request: HttpRequest, store: Store, relative: str
) -> HttpResponseBase:
    """Prefer direct private S3 delivery; internal legacy MinIO stays proxied."""
    files = getattr(store, "files", None)
    url = files.media_url(relative) if files else None
    if url:
        response = HttpResponseRedirect(url, preserve_request=True)
        response["Referrer-Policy"] = "no-referrer"
        return response
    return stream_response(request, store, relative)


def stream_response(
    request: HttpRequest, store: Store, relative: str
) -> HttpResponseBase:
    """Stream authorized ranges without materializing any local file."""
    size = store.media_size(relative)
    start, end = 0, size - 1
    status = 200
    if request.headers.get("Range"):
        try:
            start, end = parse_range(request.headers["Range"], size)
        except ValueError:
            response = JsonResponse({"error": "Invalid range"}, status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        status = 206

    chunks = store.media_chunks(relative, start, end)
    response = StreamingHttpResponse(
        async_chunks(chunks) if isinstance(request, ASGIRequest) else chunks,
        status=status,
        content_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
    )
    response["Content-Length"] = str(end - start + 1)
    response["Accept-Ranges"] = "bytes"
    if status == HTTPStatus.PARTIAL_CONTENT:
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    return response
