"""Launch heavy inference in the isolated, pinned detector environment."""

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from django.conf import settings

from apps.video_analysis.engine.clip_contract import MAX_RUNTIME_SECONDS, ClipOptions
from apps.video_analysis.engine.clip_models import failure_message
from apps.video_analysis.engine.clips import directory
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.vision import artifact

from .replay import ReplaySection


def propose(store: Store, match_id: str, weights: str) -> None:
    """Snapshot current revisions so later human corrections make proposals stale."""
    data = store.read()
    for match in data["matches"]:
        if match["id"] == match_id:
            if match.get("video"):
                store.media(match["video"])
            for frame in match["frames"]:
                store.media(frame["image"])
    with tempfile.TemporaryDirectory(prefix="inference-", dir=store.root) as directory:
        input_path = Path(directory) / "input.json"
        atomic_json(input_path, data)
        subprocess.run(
            [
                settings.VIDEO_ANALYSIS_PYTHON,
                "-m",
                "apps.video_analysis.engine.detect",
                str(store.root),
                str(input_path),
                match_id,
                weights,
            ],
            check=True,
            timeout=1800,
        )


def clip(
    store: Store,
    run_id: str,
    payload: dict,
    *,
    progress: Callable[[dict], None] | None = None,
) -> None:
    """Stage a fresh database projection and bound inference in a subprocess.

    Raises:
        ValueError: The request or input does not satisfy this operation.
        OSError: The worker process cannot start.
        SubprocessError: The worker fails or exceeds its runtime limit.

    """
    if payload.get("recording_end"):
        ReplaySection(store, run_id, payload).run(clip)
        return
    data = store.read()
    match = next((m for m in data["matches"] if m["id"] == payload["match_id"]), None)
    if match is None:
        raise ValueError("Recording no longer exists")
    options = ClipOptions.parse(payload["options"])
    options.for_recording(match)
    store.media(match["video"])
    weights = artifact(store, "runs", payload["model"]) / "fit/weights/best.pt"
    weights = store.media(weights.relative_to(store.root).as_posix())
    with tempfile.TemporaryDirectory(prefix="clip-", dir=store.root) as temporary:
        source = Path(temporary) / "input.json"
        atomic_json(
            source,
            dict(
                payload,
                run_id=run_id,
                weights=str(weights),
                match={k: v for k, v in match.items() if k != "frames"},
            ),
        )
        try:
            command = [
                settings.VIDEO_ANALYSIS_PYTHON,
                "-m",
                "apps.video_analysis.engine.clips",
                str(store.root),
                str(source),
            ]
            environment = {
                **os.environ,
                "OMP_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
            }
            if progress is None:
                subprocess.run(
                    command,
                    check=True,
                    timeout=MAX_RUNTIME_SECONDS + 60,
                    env=environment,
                )
            else:
                run_with_progress(
                    command,
                    environment,
                    directory(store, run_id) / "run.json",
                    progress,
                )
        except (subprocess.SubprocessError, OSError):
            marker = directory(store, run_id) / "run.json"
            record = (
                json.loads(marker.read_text(encoding="utf-8"))
                if marker.exists()
                else {"id": run_id, "recipe": payload, "frames": 0, "chunks": []}
            )
            record.update(
                status="failed",
                finished_at=datetime.now(UTC).isoformat(),
                message=failure_message(
                    record, "Clip worker stopped; any published frames are retained"
                ),
            )
            atomic_json(marker, record)
            raise
        finally:
            # Retain completed chunks and failure receipts if inference fails.
            root = directory(store, run_id)
            if root.exists() and progress is None:
                for path in root.glob("*.json"):
                    store.publish_artifact(path.relative_to(store.root).as_posix())


def run_with_progress(
    command: list[str],
    environment: dict,
    marker: Path,
    progress: Callable[[dict], None],
) -> None:
    """Publish complete child chunks while bounding and owning its subprocess.

    Raises:
        CalledProcessError: The worker exits unsuccessfully.
        TimeoutExpired: The worker exceeds its existing time limit.

    """
    deadline = time.monotonic() + MAX_RUNTIME_SECONDS + 60
    with subprocess.Popen(command, env=environment) as child:
        try:
            while child.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, MAX_RUNTIME_SECONDS + 60)
                with suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=min(5, remaining))
                if marker.is_file():
                    progress(json.loads(marker.read_text(encoding="utf-8")))
            if child.returncode:
                raise subprocess.CalledProcessError(child.returncode, command)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
