"""Freeze fresh-match proposal inputs and run training followed by draft labeling."""

from dataclasses import replace
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from apps.video_analysis.engine.store import Store, frame_version
from apps.video_analysis.engine.training import (
    MAX_PROPOSALS,
    RunOptions,
    proposals,
    select_candidates,
    train,
)
from apps.video_analysis.engine.vision import digest


MAX_PIPELINE_FRAMES = 1000


def prepare_inputs(
    store: Store, snapshot: dict, count: int
) -> tuple[dict, dict[str, Path], list[dict]]:
    """Freeze exactly the requested number of unique frames from new matches.

    Raises:
        ValueError: If there are too few eligible frames or invalid bounds.

    """
    if not 1 <= count <= MAX_PIPELINE_FRAMES:
        raise ValueError("Combined jobs require 1-1000 proposal frames")
    known_groups = set(snapshot["splits"])
    known_videos = {r.get("video_sha256") for r in snapshot["frames"]}
    seen = {r["image_sha256"] for r in snapshot["frames"]}
    matches: dict[str, Any] = {}
    files: dict[str, Path] = {}
    identities = []
    for match, frame in select_candidates(store, RunOptions(spacing=2)):
        if match.get("split_group", match["id"]) in known_groups or (
            match.get("video_sha256") and match["video_sha256"] in known_videos
        ):
            continue
        source = store.media(frame["image"])
        image_hash = digest(source)
        if image_hash in seen:
            continue
        seen.add(image_hash)
        # Preserve the frame exactly: review conflict fingerprints include metadata.
        item = matches.setdefault(match["id"], dict(match, frames=[]))
        item["frames"].append(frame)
        relative = source.relative_to(store.root).as_posix()
        files["data/" + relative] = source
        identities.append({
            "match_id": match["id"],
            "frame_id": frame["id"],
            "frame_version": frame_version(frame),
            "image_sha256": image_hash,
        })
        if len(identities) == count:
            break
    if len(identities) != count:
        raise ValueError(
            f"Need {count} unique pending frames from new matches; "
            f"found {len(identities)}. Import more footage before allocating a GPU."
        )
    return (
        {"schema_version": 1, "revision": 0, "matches": list(matches.values())},
        files,
        identities,
    )


def run(
    store: Store, snapshot: str, weights: str, options: RunOptions, count: int
) -> None:
    """Use the best checkpoint for bounded proposal batches on the same GPU.

    Raises:
        ValueError: If labeling does not produce the complete frozen frame set.

    """
    ensure_tracking_runtime()
    trained = train(store, snapshot, weights, options)
    checkpoint = store.root / "vision/runs" / trained["id"] / trained["checkpoint"]
    remaining = count
    while remaining:
        size = min(remaining, MAX_PROPOSALS)
        result = proposals(
            store, str(checkpoint), replace(options, limit=size, spacing=0)
        )
        if result["frames"] != size:
            raise ValueError("Labeling did not cover the frozen proposal batch")
        remaining -= size


def ensure_tracking_runtime() -> None:
    """Install pinned tracking extras in older GPU images under the job deadline."""
    target = Path.cwd() / ".tracking-runtime"
    sys.path.insert(0, str(target))
    required = {"scipy": "1.16.3", "lap": "0.5.12"}
    for name, version in required.items():
        try:
            if importlib.metadata.version(name) == version:
                continue
        except importlib.metadata.PackageNotFoundError:
            pass
        subprocess.run(
            [
                shutil.which("uv") or "uv",
                "pip",
                "install",
                "--target",
                str(target),
                "--no-deps",
                f"{name}=={version}",
            ],
            check=True,
            timeout=120,
        )


if __name__ == "__main__":
    run(
        Store(Path("data")),
        sys.argv[1],
        sys.argv[2],
        RunOptions(**json.loads(sys.argv[3])),
        int(sys.argv[4]),
    )
