"""Run roster-naming re-solves in the isolated, pinned vision environment."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

from django.conf import settings

from apps.video_analysis.engine.clips import directory
from apps.video_analysis.engine.store import Store, atomic_json


# Classifier/assignment re-solves take seconds; crops decode a few dozen frames.
TIMEOUT_SECONDS = 240


def solve(store: Store, run_id: str, request: dict) -> dict:
    """Apply queued answers to one server-selected clip run's private cache.

    Returns:
        The engine's compact review snapshot and answer receipts.

    """
    with tempfile.TemporaryDirectory(prefix="identity-", dir=store.root) as temporary:
        source = Path(temporary) / "request.json"
        output = Path(temporary) / "output.json"
        atomic_json(
            source,
            {
                **request,
                "run_directory": str(directory(store, run_id)),
                "output": str(output),
            },
        )
        subprocess.run(
            [
                settings.VIDEO_ANALYSIS_PYTHON,
                "-m",
                "apps.video_analysis.engine.clip_identity_review",
                str(source),
            ],
            check=True,
            timeout=TIMEOUT_SECONDS,
            env={**os.environ, "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2"},
        )
        return json.loads(output.read_text(encoding="utf-8"))
