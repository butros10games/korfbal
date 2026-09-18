"""Launch heavy inference in the isolated, pinned detector environment."""

from pathlib import Path
import subprocess
import tempfile

from django.conf import settings

from apps.video_analysis.engine.store import Store, atomic_json


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
