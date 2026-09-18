"""Stage paid jobs locally; only the separate controller holds cloud credentials."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from django.conf import settings

from apps.video_analysis.engine import monitor
from apps.video_analysis.engine.remote.adapters import Runpod
from apps.video_analysis.engine.remote.controller import TERMINAL, Controller, Policy
from apps.video_analysis.engine.store import ConflictError, Store
from apps.video_analysis.engine.training import RunOptions


def configured_policy() -> Policy:
    """Load the operator's read-only, credential-free policy."""
    policy = Policy(
        **json.loads(
            Path(settings.VIDEO_ANALYSIS_TRAINING_POLICY).read_text(encoding="utf-8")
        )
    )
    policy.validate()
    return policy


def policy_version(policy: Policy) -> str:
    """Bind a browser submission to the limits displayed before its click."""
    return hashlib.sha256(
        json.dumps(asdict(policy), sort_keys=True).encode()
    ).hexdigest()


def launch_status(store: Store) -> dict:
    """Expose budget and readiness without registry credentials or filesystem paths."""
    try:
        policy = configured_policy()
    except (OSError, ValueError, TypeError):
        return {"ready": False, "reason": "GPU training is not configured."}
    summary = monitor.summary(store, include_recordings=False)
    controller = summary["controller"]
    busy = any(
        j["source"] == "cloud" and j["status"] not in TERMINAL for j in summary["jobs"]
    )
    ready = (
        policy.enabled
        and all(
            controller.get(k)
            for k in ("fresh", "healthy", "enabled", "cloud_configured")
        )
        and not busy
    )
    return {
        "ready": ready,
        "reason": "A GPU job is already queued or running."
        if busy
        else ""
        if ready
        else "The training controller is paused or unavailable.",
        "gpu": policy.gpu,
        "max_seconds": policy.max_seconds,
        "max_hourly_usd": policy.max_hourly_usd,
        "policy_version": policy_version(policy),
    }


def accepted_policy(store: Store, version: str) -> dict:
    """Recheck readiness and the displayed budget before accepting a paid request.

    Raises:
        ConflictError: The controller is busy, unavailable, or its limits changed.

    """
    status = launch_status(store)
    if not status["ready"]:
        raise ConflictError(status["reason"])
    policy = configured_policy()
    if not status["ready"] or version != policy_version(policy):
        raise ConflictError(
            status["reason"] or "Training limits changed. Refresh before starting."
        )
    return asdict(policy)


def queue_training(store: Store, request_id: str, payload: dict) -> dict:
    """Idempotently publish a frozen request for the controller to execute."""
    controller = Controller(store, Runpod(""), None, Policy(**payload["policy"]))
    return controller.enqueue(
        payload["snapshot"],
        RunOptions(device="0", epochs=payload["epochs"], imgsz=960, batch=4),
        "run:" + payload["parent_run"] if payload.get("parent_run") else "yolo26n.pt",
        request_id=request_id,
    )


def cancel_training(store: Store, job_id: str) -> None:
    """Request cleanup without giving web processes access to the provider."""
    Controller(store, Runpod(""), None, configured_policy()).cancel(job_id)
