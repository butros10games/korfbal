"""Persistent reconciliation: queue -> provision -> collect -> delete -> finish."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from http import HTTPStatus
import json
import math
from operator import itemgetter
from pathlib import Path
import re
import time
from typing import Any, Protocol
import uuid
import zipfile

from apps.video_analysis.engine.coverage import dataset_report
from apps.video_analysis.engine.handoff import package_snapshot
from apps.video_analysis.engine.recovery import training_lease
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.training import (
    MAX_BATCH,
    MAX_EPOCHS,
    PRETRAINED_WEIGHTS,
    RunOptions,
    validate_options,
)
from apps.video_analysis.engine.vision import artifact, digest, identifier

from .adapters import ProviderHTTPError
from .pipeline import MAX_PIPELINE_FRAMES
from .results import import_proposals, import_result


TERMINAL = {"completed", "failed", "cancelled"}
MIN_SECONDS = 120
MAX_SECONDS = 12 * 3600
MAX_RESULT_BYTES = 512 * 1024 * 1024
RECEIPT_BYTES = 16384


class Provider(Protocol):
    """Capability boundary for paid compute."""

    def list_pods(self) -> list[dict]:
        """List provider pods."""
        ...

    def create(self, payload: dict) -> dict:
        """Create one pod."""
        ...

    def delete(self, pod_id: str) -> None:
        """Delete one pod."""
        ...


class Artifacts(Protocol):
    """Capability boundary for durable transfers."""

    def upload(self, key: str, path: Path) -> None:
        """Upload one object."""
        ...

    def url(self, key: str, operation: str, expires: int) -> str:
        """Sign an object operation."""
        ...

    def read(self, key: str, limit: int) -> bytes | None:
        """Read a bounded object."""
        ...


@dataclass(frozen=True)
class Policy:
    """Operator configuration, never supplied by a GPU worker."""

    image: str
    gpu: str = "NVIDIA RTX A5000"
    max_seconds: int = 3600
    max_hourly_usd: float = 1.0
    enabled: bool = False

    def validate(self) -> None:
        """Require an immutable worker image and bounded execution settings.

        Raises:
            ValueError: If configuration cannot bound or reproduce a launch.

        """
        if (
            not re.fullmatch(r".+@sha256:[0-9a-f]{64}", self.image)
            or self.image.endswith("@sha256:" + "0" * 64)
            or "YOUR_REGISTRY" in self.image
            or not self.gpu
        ):
            raise ValueError("Pin a worker image by digest and select one GPU type")
        if not MIN_SECONDS <= self.max_seconds <= MAX_SECONDS:
            raise ValueError("Runtime limit must be 120 seconds to 12 hours")
        if not math.isfinite(self.max_hourly_usd) or self.max_hourly_usd <= 0:
            raise ValueError("Hourly price limit must be finite and positive")


class Controller:
    """One active job; restart-safe state and exclusive local process lease."""

    def __init__(
        self,
        store: Store,
        provider: Provider,
        artifacts: Artifacts | None,
        policy: Policy,
    ) -> None:
        """Wire durable state to operator-provided outbound capabilities."""
        policy.validate()
        self.store, self.provider, self.artifacts, self.policy = (
            store,
            provider,
            artifacts,
            policy,
        )
        self.root = store.root / "vision/remote"
        self.root.mkdir(parents=True, exist_ok=True)

    def records(self) -> list[dict[str, Any]]:
        """Read job metadata only, without credentials or signed URLs."""
        return sorted(
            (json.loads(p.read_text()) for p in self.root.glob("*/job.json")),
            key=itemgetter("created_at"),
        )

    def save(self, job: dict) -> None:
        """Persist transitions before issuing external mutations."""
        atomic_json(self.root / job["id"] / "job.json", job)

    def enqueue(
        self,
        snapshot: str,
        options: RunOptions,
        weights: str,
        request_id: str | None = None,
        proposal_count: int = 0,
    ) -> dict:
        """Freeze the transfer kit; enqueueing itself never rents a machine.

        Raises:
            ValueError: If the requested training recipe is unsupported.

        """
        validate_options(options)
        parent_run = weights.removeprefix("run:") if weights.startswith("run:") else ""
        if not 0 <= proposal_count <= MAX_PIPELINE_FRAMES:
            raise ValueError("Proposal count must be 0-1000")
        if (
            (not parent_run and weights not in PRETRAINED_WEIGHTS)
            or options.device != "0"
            or not 1 <= options.epochs <= MAX_EPOCHS
            or not 1 <= options.batch <= MAX_BATCH
        ):
            raise ValueError(
                "Use supported pretrained weights, device 0 and bounded epochs/batch"
            )
        dataset_report(self.store, snapshot)
        job_id = uuid.UUID(request_id).hex if request_id else uuid.uuid4().hex
        with training_lease(self.root):
            root = self.root / job_id
            existing = root / "job.json"
            if existing.exists():
                job = json.loads(existing.read_text())
                if (
                    job["snapshot"] != snapshot
                    or asdict(RunOptions(**job["options"])) != asdict(options)
                    or job["weights"] != weights
                    or job["policy"] != asdict(self.policy)
                    or (job.get("proposal_count", 0), job.get("parent_run", ""))
                    != (proposal_count, parent_run)
                ):
                    raise ValueError("Training request ID already used")
                return job
            root.mkdir(exist_ok=True)
            # Recover a crash during packaging before a job was published.
            (root / "kit.zip").unlink(missing_ok=True)
            kit = package_snapshot(
                self.store, snapshot, root / "kit.zip", proposal_count, parent_run
            )
            job = {
                "id": job_id,
                "name": "korfbal-" + job_id,
                "status": "queued",
                "created_at": time.time(),
                "snapshot": snapshot,
                "kit_sha256": kit["sha256"],
                "dataset_sha256": digest(
                    artifact(self.store, "snapshots", snapshot) / "manifest.json"
                ),
                "options": asdict(options),
                "weights": weights,
                "parent_run": parent_run,
                "policy": asdict(self.policy),
                "cancel_requested": False,
                "proposal_count": proposal_count,
            }
            self.save(job)
        return job

    def cancel(self, job_id: str) -> None:
        """Persist cancellation; the reconciler still owns provider cleanup."""
        identifier(job_id)
        with training_lease(self.root):
            job = json.loads((self.root / job_id / "job.json").read_text())
            if job["status"] not in TERMINAL:
                job["cancel_requested"] = True
                self.save(job)

    def delete_owned(self, pods: list[dict]) -> None:
        """Delete only the pods already matched to an owned job."""
        for pod in pods:
            self.provider.delete(pod["id"])

    def offline(self, jobs: list[dict]) -> None:
        """Allow local staging while refusing to abandon previously owned workers.

        Raises:
            RuntimeError: If cloud access is required for scheduling or cleanup.

        """
        if self.policy.enabled or any("deadline" in job for job in jobs):
            raise RuntimeError("Cloud credentials required for scheduling or cleanup")
        for job in jobs:
            if job["cancel_requested"] and job["status"] == "queued":
                job["status"] = "cancelled"
                self.save(job)

    def tick(self, now: float | None = None) -> None:
        """Reconcile once; disabled mode still cleans up already-owned workers."""
        now = time.time() if now is None else now
        with training_lease(self.root):
            jobs = self.records()
            if self.artifacts is None:
                self.offline(jobs)
                return
            if not jobs:
                return
            pods = self.provider.list_pods()
            active = False
            for job in jobs:
                owned = [p for p in pods if p.get("name") == job["name"]]
                # Also reclaim late creates and duplicate owned pods after finalization.
                if job["status"] in TERMINAL:
                    self.delete_owned(owned)
                    active = active or bool(owned)
                    continue
                if job["status"] == "queued":
                    if job["cancel_requested"]:
                        job["status"] = "cancelled"
                        self.save(job)
                    continue
                active = True
                try:
                    self.advance(job, owned, now)
                    job.pop("controller_error", None)
                except (OSError, ValueError, RuntimeError, KeyError) as error:
                    # Exception text can contain signed URLs or provider credentials.
                    job["controller_error"] = type(error).__name__
                self.save(job)
            if not active and self.policy.enabled:
                queued = next((j for j in jobs if j["status"] == "queued"), None)
                if queued:
                    self.launch(queued, now)

    def launch(self, job: dict, now: float) -> None:
        """Upload first, persist create intent, then perform a single POST."""
        assert self.artifacts is not None
        key = job["id"] + "/"
        try:
            self.artifacts.upload(key + "kit.zip", self.root / job["id"] / "kit.zip")
            ttl = job["policy"]["max_seconds"] + 3600
            spec = {
                "id": job["id"],
                "snapshot": job["snapshot"],
                "kit_sha256": job["kit_sha256"],
                "options": job["options"],
                "weights": "data/base-model.pt"
                if job.get("parent_run")
                else job["weights"],
                "proposal_count": job.get("proposal_count", 0),
                "max_seconds": job["policy"]["max_seconds"],
                "deadline": now + job["policy"]["max_seconds"],
                "kit_url": self.artifacts.url(key + "kit.zip", "get_object", ttl),
                "result_url": self.artifacts.url(key + "result.zip", "put_object", ttl),
                "receipt_url": self.artifacts.url(
                    key + "receipt.json", "put_object", ttl
                ),
            }
            job.update(
                status="provisioning", deadline=now + job["policy"]["max_seconds"]
            )
            self.save(job)
            pod = self.provider.create({
                "name": job["name"],
                "imageName": job["policy"]["image"],
                "computeType": "GPU",
                "cloudType": "SECURE",
                "gpuCount": 1,
                "gpuTypeIds": [job["policy"]["gpu"]],
                "allowedCudaVersions": ["13.0"],
                "interruptible": False,
                "containerDiskInGb": 40,
                "volumeInGb": 0,
                "ports": [],
                "dockerEntrypoint": [
                    "/opt/vision/bin/python",
                    "-m",
                    "scripts.python.korfbal_review.remote.worker",
                ],
                "dockerStartCmd": [],
                "env": {"KORFBAL_JOB": json.dumps(spec)},
            })
            job["pod_id"] = pod["id"]
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            job["controller_error"] = type(error).__name__
            job["launch_error"] = type(error).__name__
            if isinstance(error, ProviderHTTPError):
                job["provider_http_status"] = error.status
                if (
                    HTTPStatus.BAD_REQUEST
                    <= error.status
                    < HTTPStatus.INTERNAL_SERVER_ERROR
                    and error.status != HTTPStatus.REQUEST_TIMEOUT
                ):
                    job.update(
                        status="cleanup", outcome="failed", reason="provider_rejected"
                    )
        self.save(job)

    def advance(self, job: dict, pods: list[dict], now: float) -> None:
        """Reconcile receipt, runtime limit, cancellation and confirmed deletion."""
        if job["status"] == "cleanup":
            for pod in pods:
                self.provider.delete(pod["id"])
            if not pods:
                job.update(status=job["outcome"], finished_at=now)
            return
        if job["cancel_requested"] or now >= job["deadline"]:
            job.update(
                status="cleanup",
                outcome="cancelled" if job["cancel_requested"] else "failed",
                reason="cancelled" if job["cancel_requested"] else "runtime_limit",
            )
            for pod in pods:
                self.provider.delete(pod["id"])
            return
        if len(pods) > 1:
            job.update(status="cleanup", outcome="failed", reason="duplicate_workers")
            return
        if pods:
            price = float(pods[0].get("costPerHr", "nan"))
            if not math.isfinite(price) or price > job["policy"]["max_hourly_usd"]:
                job.update(
                    status="cleanup", outcome="failed", reason="hourly_price_limit"
                )
                self.provider.delete(pods[0]["id"])
                return
            job.update(status="running", pod_id=pods[0]["id"], hourly_usd=price)
        assert self.artifacts is not None
        receipt = self.artifacts.read(job["id"] + "/receipt.json", RECEIPT_BYTES)
        if receipt is not None:
            self.collect(job, json.loads(receipt))

    def collect(self, job: dict, receipt: dict) -> None:
        """Keep a verified result locally before requesting destructive cleanup.

        Raises:
            ValueError: If result provenance or its checksum does not match.

        """
        if (
            receipt.get("id") != job["id"]
            or receipt.get("kit_sha256") != job["kit_sha256"]
        ):
            raise ValueError("Result does not belong to this job")
        assert self.artifacts is not None
        content = self.artifacts.read(job["id"] + "/result.zip", MAX_RESULT_BYTES)
        if content is None or hashlib.sha256(content).hexdigest() != receipt.get(
            "sha256"
        ):
            raise ValueError("Result upload is missing or damaged")
        root = self.root / job["id"]
        temporary = root / "result.tmp"
        temporary.write_bytes(content)
        temporary.replace(root / "result.zip")
        atomic_json(root / "receipt.json", receipt)
        if receipt.get("status") == "completed" or receipt.get("training_completed"):
            job["imported_run"] = import_result(self.store, job, content)
        outcome = "completed" if receipt.get("status") == "completed" else "failed"
        if outcome == "completed" and job.get("proposal_count"):
            try:
                job["imported_proposals"] = import_proposals(self.store, job, content)
            except (ValueError, KeyError, zipfile.BadZipFile):
                outcome = "failed"
                job["reason"] = "proposal_verification_failed"
        job.update(
            status="cleanup",
            outcome=outcome,
            result_sha256=receipt["sha256"],
        )
