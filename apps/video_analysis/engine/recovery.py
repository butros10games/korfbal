"""Training leases, interruption recovery and comparable evaluation reports."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import importlib
import json
from pathlib import Path
import time
from typing import Any

from .store import Store, atomic_json
from .vision import artifact, digest, verify_snapshot


@contextmanager
def training_lease(root: Path) -> Iterator[None]:
    """Prevent two processes from training or resuming the same run concurrently.

    Raises:
        ValueError: If another trainer holds the run lease.

    """
    with (root / ".training.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("This training run is already active") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def resume(store: Store, run_id: str, device: str = "cpu") -> dict[str, Any]:
    """Continue an interrupted local run with optimizer state and its original dataset.

    Raises:
        ValueError: If the run is complete, invalid or its dataset changed.
        KeyboardInterrupt: If the user interrupts resumed training.

    """
    # Import through the module to keep the optional detector lazily loaded.
    training = importlib.import_module("scripts.python.korfbal_review.training")

    root = artifact(store, "runs", run_id)
    with training_lease(root):
        run = json.loads((root / "run.json").read_text())
        if run["kind"] != "train" or run["status"] == "completed":
            raise ValueError("Choose an interrupted training run, not a completed run")
        dataset = artifact(store, "snapshots", run["snapshot"])
        manifest = verify_snapshot(dataset)
        if digest(dataset / "manifest.json") != run["dataset_sha256"]:
            raise ValueError(
                "The original training dataset changed; start a new experiment"
            )
        checkpoint = root / "fit/weights/last.pt"
        if not checkpoint.is_file():
            raise ValueError(
                "No last.pt checkpoint exists yet; start a new training run"
            )
        attempt = {
            "started_at": datetime.now(UTC).isoformat(),
            "previous_status": run["status"],
            "checkpoint_sha256": digest(checkpoint),
            "device": device,
        }
        run.setdefault("resume_attempts", []).append(attempt)
        run.update(status="running")
        run.pop("error", None)
        atomic_json(root / "run.json", run)
        started = time.monotonic()
        try:
            model = training.detector(str(checkpoint))
            atomic_json(
                root / "data.yaml",
                {
                    "path": str(dataset),
                    "train": "images/train",
                    "val": "images/val",
                    "names": manifest["classes"],
                },
            )
            # Explicit paths also support a run relocated together with its snapshot.
            model.train(
                resume=True,
                device=device,
                data=str(root / "data.yaml"),
                save_dir=str(root / "fit"),
            )
            best = root / "fit/weights/best.pt"
            if not best.is_file():
                raise ValueError("Resume completed without a best checkpoint")
            run.update(
                status="completed",
                checkpoint="fit/weights/best.pt",
                checkpoint_sha256=digest(best),
            )
        except (Exception, KeyboardInterrupt) as error:
            run.update(
                status="interrupted"
                if isinstance(error, KeyboardInterrupt)
                else "failed",
                error=f"{type(error).__name__}: {error}",
            )
            raise
        finally:
            attempt.update(
                status=run["status"],
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            atomic_json(root / "run.json", run)
    return run


def compare_runs(store: Store, baseline: str, candidate: str) -> dict[str, Any]:
    """Compare like-for-like evaluations without choosing a model automatically.

    Raises:
        ValueError: If reports have different evaluation data or metric settings.

    """
    runs = [
        json.loads((artifact(store, "runs", name) / "run.json").read_text())
        for name in (baseline, candidate)
    ]
    first, second = runs
    if any(
        run.get("kind") != "evaluate" or run.get("status") != "completed"
        for run in runs
    ):
        raise ValueError("Select two completed evaluation runs")
    for key in ("dataset_sha256", "split", "metric_definition", "frames"):
        if first.get(key) is None or first.get(key) != second.get(key):
            raise ValueError(f"Evaluation reports are not comparable: {key}")
    for key in ("imgsz", "confidence", "iou"):
        if first["config"].get(key) is None or first["config"].get(key) != second[
            "config"
        ].get(key):
            raise ValueError(f"Evaluation settings differ: {key}")
    if first["metrics"].keys() != second["metrics"].keys():
        raise ValueError("Evaluated classes differ")
    classes = {}
    for label, before in first["metrics"].items():
        after = second["metrics"][label]
        classes[label] = {
            "baseline": before,
            "candidate": after,
            "delta": {
                metric: after[metric] - before[metric]
                if before[metric] is not None and after[metric] is not None
                else None
                for metric in ("precision", "recall")
            },
        }
    return {
        "baseline": baseline,
        "candidate": candidate,
        "snapshot": first["snapshot"],
        "split": first["split"],
        "classes": classes,
        "note": (
            "Positive deltas favor the candidate. Check per-class tradeoffs "
            "and review time; this report does not promote a model automatically."
        ),
    }
