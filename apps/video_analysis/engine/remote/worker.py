"""One-job worker: download a verified kit, train, upload results, then exit."""

from __future__ import annotations

import hashlib
from http import HTTPStatus
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import zipfile

from apps.video_analysis.engine.handoff import verify_kit
from apps.video_analysis.engine.store import atomic_json
from apps.video_analysis.engine.vision import digest

from .transport import request


MAX_KIT_BYTES = 8 * 1024**3
MAX_RESULT_BYTES = 512 * 1024**2
CHUNK_BYTES = 1024**2
UPLOAD_ATTEMPTS = 3


def download_kit(spec: dict, root: Path) -> Path:
    """Verify bytes before extracting any executable source.

    Raises:
        ValueError: If the kit is oversized, damaged or contains unsafe paths.
        RuntimeError: If the artifact endpoint rejects the download.

    """
    archive_path = root / "kit.zip"
    with (
        request(spec["kit_url"]) as response,
        archive_path.open("wb") as output,
    ):
        if response.status != HTTPStatus.OK:
            raise RuntimeError("Kit download failed")
        total = 0
        while chunk := response.read(CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_KIT_BYTES:
                raise ValueError("Kit too large")
            output.write(chunk)
    if digest(archive_path) != spec["kit_sha256"]:
        raise ValueError("Kit checksum mismatch")
    extracted = root / "kit"
    extracted.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_KIT_BYTES:
            raise ValueError("Expanded kit too large")
        for info in archive.infolist():
            path = (extracted / info.filename).resolve()
            if not path.is_relative_to(extracted.resolve()):
                raise ValueError("Unsafe kit path")
        archive.extractall(extracted)
    verify_kit(extracted)
    return extracted


def execute(spec: dict, extracted: Path, log: Path) -> bool:
    """Run the frozen kit's trainer with a process-group timeout."""
    options = spec["options"]
    command = [
        sys.executable,
        "-m",
        "scripts.python.korfbal_vision",
        "--data",
        "data",
        "train",
        spec["snapshot"],
        "--weights",
        spec["weights"],
    ]
    for key in ("device", "epochs", "imgsz", "batch", "workers", "seed", "cache"):
        command.extend(["--" + key, str(options[key])])
    for key in ("optimizer", "learning_rate", "freeze", "patience", "close_mosaic"):
        if options.get(key) is not None:
            command.extend(["--" + key.replace("_", "-"), str(options[key])])
    if spec.get("proposal_count"):
        command = [
            sys.executable,
            "-m",
            "scripts.python.korfbal_review.remote.pipeline",
            spec["snapshot"],
            spec["weights"],
            json.dumps(options),
            str(spec["proposal_count"]),
        ]
    environment = dict(os.environ)
    environment.pop("KORFBAL_JOB", None)
    environment.pop("PYTHONPATH", None)
    with log.open("wb") as output:
        process = subprocess.Popen(
            command,
            cwd=extracted,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return (
                process.wait(timeout=max(1, spec["deadline"] - time.time() - 60)) == 0
            )
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return False


def upload(url: str, content: bytes) -> None:
    """Retry idempotent writes to job-specific artifact objects.

    Raises:
        OSError: If all upload attempts fail.

    """
    for attempt in range(UPLOAD_ATTEMPTS):
        try:
            with request(url, "PUT", content) as response:
                if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
                    raise OSError("Artifact upload failed")
                return
        except OSError:
            if attempt == UPLOAD_ATTEMPTS - 1:
                raise
            time.sleep(2**attempt)


def run(spec: dict) -> None:
    """Publish results or bounded failure diagnostics before the cleanup signal."""
    with tempfile.TemporaryDirectory(prefix="korfbal-worker-") as temporary:
        root = Path(temporary)
        receipt = {
            "id": spec["id"],
            "kit_sha256": spec["kit_sha256"],
            "status": "failed",
        }
        try:
            extracted = download_kit(spec, root)
            if execute(spec, extracted, root / "train.log"):
                receipt["status"] = "completed"
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            receipt["error"] = type(error).__name__
        # A labeling error must not discard successful training from the same job.
        receipt["training_completed"] = any(
            (record := json.loads(path.read_text())).get("kind") == "train"
            and record.get("status") == "completed"
            for path in root.glob("kit/data/vision/runs/*/run.json")
        )
        archive_path = root / "result.zip"
        atomic_json(root / "worker.json", receipt)
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if path.is_file() and (
                    path.name in {"worker.json", "train.log"}
                    or (
                        "runs" in path.parts
                        and (
                            path.suffix in {".json", ".csv", ".yaml"}
                            or path.name in {"best.pt", "last.pt", "numbers.pt"}
                        )
                    )
                ):
                    archive.write(path, str(path.relative_to(root)))
        if archive_path.stat().st_size > MAX_RESULT_BYTES:
            # Report terminal failure so the controller releases the paid pod.
            # Raising here lets the provider restart and repeat the entire run.
            receipt.update(
                status="failed", training_completed=False, error="ResultTooLarge"
            )
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("worker.json", json.dumps(receipt))
        content = archive_path.read_bytes()
        upload(spec["result_url"], content)
        receipt["sha256"] = hashlib.sha256(content).hexdigest()
        upload(spec["receipt_url"], json.dumps(receipt).encode())


if __name__ == "__main__":
    run(json.loads(os.environ["KORFBAL_JOB"]))
