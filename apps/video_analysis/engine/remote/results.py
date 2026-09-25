"""Validate returned training artifacts before making a checkpoint reviewable."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import zipfile

from apps.video_analysis.engine.store import Store, atomic_json, validate_annotation
from apps.video_analysis.engine.vision import artifact, digest, identifier


MAX_EXPANDED_BYTES = 1024**3


def import_result(store: Store, job: dict, content: bytes) -> str:
    """Import only a completed matching run and explicitly allowed result files.

    Raises:
        ValueError: If the run, snapshot or checkpoint cannot be verified.

    """
    name = "remote-" + job["id"]
    target = artifact(store, "runs", name)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sum(i.file_size for i in archive.infolist()) > MAX_EXPANDED_BYTES:
            raise ValueError("Expanded result is too large")
        manifests = [
            n
            for n in archive.namelist()
            if n.endswith("/run.json")
            and json.loads(archive.read(n)).get("kind") == "train"
        ]
        if len(manifests) != 1:
            raise ValueError("Expected exactly one training result")
        run = json.loads(archive.read(manifests[0]))
        if (
            run.get("kind") != "train"
            or run.get("status") != "completed"
            or run.get("snapshot") != job["snapshot"]
            or run.get("dataset_sha256") != job["dataset_sha256"]
            or run.get("checkpoint") != "fit/weights/best.pt"
        ):
            raise ValueError("Returned training provenance does not match")
        prefix = manifests[0].removesuffix("run.json")
        best = archive.read(prefix + "fit/weights/best.pt")
        if hashlib.sha256(best).hexdigest() != run.get("checkpoint_sha256"):
            raise ValueError("Returned checkpoint checksum does not match")
        if target.exists():
            existing = json.loads((target / "run.json").read_text())
            if existing.get("checkpoint_sha256") != run["checkpoint_sha256"]:
                raise ValueError("A different result was already imported")
            return name
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
            staging = Path(temporary) / name
            (staging / "fit/weights").mkdir(parents=True)
            (staging / "fit/weights/best.pt").write_bytes(best)
            for relative in ("fit/weights/last.pt", "fit/results.csv"):
                if prefix + relative in archive.namelist():
                    (staging / relative).write_bytes(archive.read(prefix + relative))
            copy_reader(archive, prefix, run, staging)
            run.update(id=name, remote_job=job["id"], remote_training_id=run["id"])
            atomic_json(staging / "run.json", run)
            staging.rename(target)
    return name


def copy_reader(
    archive: zipfile.ZipFile, prefix: str, run: dict, staging: Path
) -> None:
    """Keep a returned shirt-number reader only when it matches its record.

    Raises:
        ValueError: If the reader bytes differ from the recorded checksum.

    """
    name = prefix + "fit/weights/numbers.pt"
    if name not in archive.namelist():
        return
    reader = archive.read(name)
    if hashlib.sha256(reader).hexdigest() != run.get("numbers", {}).get("sha256"):
        raise ValueError("Returned number reader checksum does not match")
    (staging / "fit/weights/numbers.pt").write_bytes(reader)


def import_proposals(store: Store, job: dict, content: bytes) -> list[str]:
    """Verify complete frozen coverage and checkpoint provenance before publication.

    Raises:
        ValueError: If proposals are incomplete, duplicated or belong to other inputs.

    """
    kit_path = store.root / "vision/remote" / job["id"] / "kit.zip"
    if digest(kit_path) != job["kit_sha256"]:
        raise ValueError("Frozen proposal kit changed")
    with zipfile.ZipFile(kit_path) as kit:
        expected = json.loads(kit.read("kit.json"))["proposal_frames"]
    training = json.loads(
        (artifact(store, "runs", job["imported_run"]) / "run.json").read_text()
    )
    seen = []
    prepared = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sum(i.file_size for i in archive.infolist()) > MAX_EXPANDED_BYTES:
            raise ValueError("Expanded result is too large")
        for name in archive.namelist():
            if not name.endswith("/run.json"):
                continue
            run = json.loads(archive.read(name))
            if run.get("kind") != "propose":
                continue
            if (
                run.get("status") != "completed"
                or run.get("weights_sha256") != training["checkpoint_sha256"]
            ):
                raise ValueError("Proposal checkpoint provenance does not match")
            report = json.loads(
                archive.read(name.removesuffix("run.json") + "predictions.json")
            )
            seen.extend(proposal_identities(run, report))
            run_id = "remote-" + job["id"] + "-" + identifier(run["id"])
            run.update(
                id=run_id, remote_job=job["id"], training_run=job["imported_run"]
            )
            report["run"] = run_id
            prepared.append((run_id, run, report))

    def canonical(rows: list[dict]) -> list[str]:
        return sorted(json.dumps(row, sort_keys=True) for row in rows)

    if (
        len(expected) != job["proposal_count"]
        or canonical(seen) != canonical(expected)
        or len({r[0] for r in prepared}) != len(prepared)
    ):
        raise ValueError("Returned proposals do not cover the frozen frames exactly")
    return publish_proposals(store, prepared)


def publish_proposals(
    store: Store, prepared: list[tuple[str, dict, dict]]
) -> list[str]:
    """Publish verified runs atomically and accept only identical retry results.

    Raises:
        ValueError: If a previous import has different proposal content.

    """
    for run_id, run, report in prepared:
        target = artifact(store, "runs", run_id)
        if target.exists():
            if json.loads((target / "predictions.json").read_text()) != report:
                raise ValueError("A different proposal result was already imported")
            continue
        with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
            staging = Path(temporary) / run_id
            atomic_json(staging / "predictions.json", report)
            atomic_json(staging / "run.json", run)
            staging.rename(target)
    return [r[0] for r in prepared]


def proposal_identities(run: dict, report: dict) -> list[dict]:
    """Validate annotation payloads and extract their immutable frame identities.

    Raises:
        ValueError: If the run metadata and returned frame count disagree.

    """
    if report.get("run") != run["id"] or len(report["frames"]) != run["frames"]:
        raise ValueError("Proposal run frame count does not match")
    identities = []
    for frame in report["frames"]:
        identities.append({
            key: frame[key]
            for key in ("match_id", "frame_id", "frame_version", "image_sha256")
        })
        validate_annotation(frame["prediction"])
        validate_annotation(frame["suggestion"])
    return identities
