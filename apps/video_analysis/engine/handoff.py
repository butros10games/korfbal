"""Portable, checksummed training kits and local machine readiness reports."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import tempfile
from typing import Any
import zipfile

from .remote.pipeline import prepare_inputs
from .store import Store
from .temporal import freeze
from .vision import artifact, digest, identifier, verify_snapshot


def doctor(store: Store, snapshot: str | None = None) -> dict[str, Any]:
    """Inspect the current machine without downloading weights or starting training."""
    torch = (
        importlib.import_module("torch") if importlib.util.find_spec("torch") else None
    )
    cuda = bool(torch is not None and torch.cuda.is_available())
    devices = []
    if cuda:
        for index in range(torch.cuda.device_count()):
            properties = torch.cuda.get_device_properties(index)
            devices.append({
                "index": index,
                "name": properties.name,
                "vram_gib": round(properties.total_memory / 1024**3, 2),
            })
    report = {
        "python": platform.python_version(),
        "platform": platform.system(),
        "torch_installed": torch is not None,
        "cuda_available": cuda,
        "ultralytics_installed": importlib.util.find_spec("ultralytics") is not None,
        "gpus": devices,
        "free_disk_gib": round(shutil.disk_usage(store.root).free / 1024**3, 2),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "notes": [
            "GPU memory and free disk describe capacity, not measured training speed."
        ],
    }
    if snapshot:
        manifest = verify_snapshot(artifact(store, "snapshots", snapshot))
        report["snapshot"] = {
            "id": snapshot,
            "counts": manifest["counts"],
            "classes": manifest["classes"],
            "integrity": "passed",
        }
    return report


def parent_checkpoint(store: Store, run_id: str) -> Path:
    """Resolve only a completed, checksum-verified checkpoint in this workspace.

    Raises:
        ValueError: The selected run or its checkpoint is invalid.

    """
    root = artifact(store, "runs", run_id)
    run = json.loads((root / "run.json").read_text())
    checkpoint = root / "fit/weights/best.pt"
    if (
        run.get("kind") != "train"
        or run.get("status") != "completed"
        or run.get("checkpoint") != "fit/weights/best.pt"
        or digest(checkpoint) != run.get("checkpoint_sha256")
    ):
        raise ValueError("Select a completed training run with a verified checkpoint")
    return checkpoint


def package_snapshot(
    store: Store,
    snapshot: str,
    output: Path,
    proposal_count: int = 0,
    parent_run: str = "",
) -> dict[str, Any]:
    """Export verified training assets and source without raw video or histories.

    Raises:
        ValueError: If the requested output already exists.

    """
    dataset = artifact(store, "snapshots", snapshot)
    manifest = verify_snapshot(dataset)
    if output.exists():
        raise ValueError("Training kit already exists; choose a new output filename")
    source = next(
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "scripts/python/korfbal_vision.py").is_file()
    )
    files = {"manifest.json", "data.yaml"}
    files.update(
        record[kind] for record in manifest["frames"] for kind in ("image", "label")
    )
    members = {
        f"data/vision/snapshots/{snapshot}/{name}": dataset / name
        for name in sorted(files)
    }
    if parent_run:
        members["data/base-model.pt"] = parent_checkpoint(store, parent_run)
    generated = {}
    proposal_frames = []
    if proposal_count:
        review, images, proposal_frames = prepare_inputs(
            store, manifest, proposal_count
        )
        generated["data/temporal.json"], context_images = freeze(store, review)
        members.update(context_images)
        members.update(images)
        generated["data/review.json"] = json.dumps(review).encode()
    for path in sorted((source / "scripts/python/korfbal_review").glob("*.py")):
        members[str(path.relative_to(source))] = path
    engine = Path(__file__).resolve().parent
    for path in engine.rglob("*.py"):
        members[
            f"apps/django_projects/korfbal/apps/video_analysis/engine/{path.relative_to(engine)}"
        ] = path
    for name in (
        "korfbal_vision.py",
        "korfbal_vision_environment.py",
        "korfbal_autotrack_dataset.py",
    ):
        members[f"scripts/python/{name}"] = source / "scripts/python" / name
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="training-kit-", dir=output.parent
    ) as temporary:
        archive_path = Path(temporary) / "kit.zip"
        checksums = {}
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in generated.items():
                archive.writestr(name, content)
                checksums[name] = hashlib.sha256(content).hexdigest()
            for name, path in members.items():
                content = path.read_bytes()
                archive.writestr(name, content)
                checksums[name] = hashlib.sha256(content).hexdigest()
            instructions = kit_instructions(snapshot)
            archive.writestr("START.txt", instructions)
            checksums["START.txt"] = hashlib.sha256(instructions.encode()).hexdigest()
            kit = {
                "schema_version": 1,
                "snapshot": snapshot,
                "counts": manifest["counts"],
                "files": checksums,
                "dataset_sha256": digest(dataset / "manifest.json"),
                "proposal_frames": proposal_frames,
                "parent_run": parent_run,
            }
            archive.writestr("kit.json", json.dumps(kit, indent=2))
        # Publish without replacing a kit, including during concurrent exports.
        output.hardlink_to(archive_path)
    return {
        "output": str(output),
        "sha256": digest(output),
        "snapshot": snapshot,
        "files": len(checksums),
    }


def kit_instructions(snapshot: str) -> str:
    """Generate machine-neutral commands for a complete first experiment."""
    prefix = ".venv/bin/python -m scripts.python.korfbal_vision --data data"
    return (
        "KorfConnect training kit. Extract all files together. Use Linux or WSL2.\n"
        "Review Ultralytics' code/weight licensing before product distribution.\n\n"
        "Check transferred files before installing dependencies:\n"
        "python -m scripts.python.korfbal_vision verify-kit .\n\n"
        "Create the GPU runtime (append --cpu for a CPU-only machine):\n"
        "python -m scripts.python.korfbal_vision_environment .venv\n\n"
        f"{prefix} doctor --snapshot {snapshot}\n"
        f"{prefix} coverage {snapshot}\n"
        f"{prefix} benchmark {snapshot} --weights yolo26n.pt "
        "--device 0 --epochs 2 --batch 2\n"
        f"{prefix} train {snapshot} --weights yolo26n.pt "
        "--device 0 --batch -1 --workers 2 --cache disk\n\n"
        "Use the same snapshot and threshold for every candidate model:\n"
        f"{prefix} evaluate {snapshot} --weights PATH_TO_BEST_PT --device 0\n"
        f"{prefix} compare BASELINE_EVALUATION_ID CANDIDATE_EVALUATION_ID\n\n"
        "Recover an interrupted run without restarting its optimizer:\n"
        f"{prefix} resume TRAIN_RUN_ID --device 0\n\n"
        "For CPU training, use --device cpu --batch 2. "
        "A kit contains corrected images, "
        "so keep it private. Raw video and unreviewed proposals are not included.\n"
    )


def verify_kit(root: Path) -> dict[str, Any]:
    """Check every transferred asset without importing the optional model runtime.

    Raises:
        ValueError: If any transferred file is missing, changed or outside the kit.

    """
    root = root.resolve()
    kit = json.loads((root / "kit.json").read_text())
    identifier(kit["snapshot"])
    for name, expected in kit["files"].items():
        path = (root / name).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or digest(path) != expected
        ):
            raise ValueError(f"Training kit integrity failed: {name}")
    dataset = root / "data/vision/snapshots" / kit["snapshot"]
    if digest(dataset / "manifest.json") != kit["dataset_sha256"]:
        raise ValueError("Training kit dataset manifest changed")
    manifest = verify_snapshot(dataset)
    return {
        "integrity": "passed",
        "snapshot": kit["snapshot"],
        "files": len(kit["files"]),
        "counts": manifest["counts"],
    }
