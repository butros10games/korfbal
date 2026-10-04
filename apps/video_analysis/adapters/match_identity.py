"""Stage a finished replay's match-pass inputs from private storage.

The match pass (``engine/clip_match_wide.py``) reads every completed section's
identity evidence and its reviewer's answers. With object storage those files
are evicted between jobs (only JSON stays on the worker), and a cold worker
has none of them, so the pass restores exactly what it needs first: each
section's receipt, evidence manifest and checksummed samples, and its review
cache. It never runs on partial inputs. A section whose receipt says it saved
evidence must have that evidence byte for byte, and a section whose answers
the database applied must have its review cache at that revision; otherwise
the pass fails with a terminal receipt instead of silently dropping a section
or a reviewer's corrections.
"""

import json
from pathlib import Path

from apps.video_analysis.engine.clip_match_wide import (
    EVIDENCE,
    MANIFEST,
    REVIEW,
    review_revision,
)
from apps.video_analysis.engine.clips import directory
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.vision import digest
from apps.video_analysis.models import ClipIdentityReview, StoredFile


INPUTS = "match-inputs.json"


class MatchInputsError(ValueError):
    """A section's evidence or answers cannot be staged; the pass must not run."""


def restore(store: Store, path: Path) -> bool:
    """Make one run artifact local, from object storage if it was evicted.

    A local copy wins: the worker published it at the start of its lease, or it
    is newer than the stored one and still waiting for publication.

    Returns:
        Whether the file is now available locally.

    """
    if path.is_file():
        return True
    workspace = getattr(store, "workspace_id", None)
    relative = path.relative_to(store.root).as_posix()
    if (
        workspace is None
        or not StoredFile.objects.filter(
            workspace_id=workspace, relative_path=relative
        ).exists()
    ):
        return False
    try:
        store.media(relative)
    except (OSError, ValueError):
        return False
    return path.is_file()


def stage(store: Store, run_id: str) -> Path:
    """Restore and verify what a replay's match pass solves, nothing more.

    Returns:
        The inputs file the engine verifies before it solves.

    Raises:
        MatchInputsError: A section's receipt, evidence or answers are
            unavailable, changed, or older than the answers already applied.

    """
    parent = directory(store, run_id)
    record = json.loads((parent / "run.json").read_text(encoding="utf-8"))
    reviews = dict(
        ClipIdentityReview.objects
        .filter(job_id=run_id)
        .exclude(section="")
        .values_list("section", "revision")
    )
    sections = []
    for part in range(int(record.get("completed_parts", 0))):
        label = f"part-{part:04d}"
        root = directory(store, f"{run_id}-{label}")
        if not restore(store, root / "run.json"):
            raise MatchInputsError(f"Section {label} is unavailable")
        child = json.loads((root / "run.json").read_text(encoding="utf-8"))
        evidence = child.get("match_evidence") or {}
        if evidence.get("status") != "saved":
            # Nothing to describe in this section (no linked tracklets).
            sections.append({"part": part})
            continue
        if not (restore(store, root / MANIFEST) and restore(store, root / EVIDENCE)):
            raise MatchInputsError(f"Section {label}'s evidence is unavailable")
        if digest(root / EVIDENCE) != evidence.get("sha256"):
            raise MatchInputsError(f"Section {label}'s identity evidence changed")
        applied = int(reviews.get(label, 0))
        staged = review_revision(root / REVIEW) if restore(store, root / REVIEW) else 0
        if staged != applied:
            raise MatchInputsError(
                f"Section {label}'s answers are at revision {staged}, "
                f"but revision {applied} was applied"
            )
        sections.append({
            "part": part,
            "evidence_sha256": evidence["sha256"],
            "review_revision": applied,
        })
    path = parent / INPUTS
    atomic_json(path, {"version": 1, "replay": run_id, "sections": sections})
    return path
