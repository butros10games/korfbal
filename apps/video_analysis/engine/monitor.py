"""Read-only, credential-free summaries for the reviewer operations screen."""

from __future__ import annotations

import json
from pathlib import Path
import time

from .store import Store, frame_version
from .vision import assignments, eligible, proposal_index


MAX_RECORD_BYTES = 2_000_000
HEARTBEAT_MAX_AGE = 60


def read_record(path: Path) -> dict | None:
    """Skip missing, incomplete or oversized status records without exposing them."""
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def summary(
    store: Store, now: float | None = None, *, include_recordings: bool = True
) -> dict:
    """Project saved review and job state; never return provider payloads or URLs."""
    now = time.time() if now is None else now
    mapping = assignments(store)
    model_drafts = {
        (r["match_id"], r["frame_id"]): r["frame_version"]
        for r in (
            proposal_index(store, "latest").values() if include_recordings else []
        )
    }
    recordings = []
    for match in store.read()["matches"] if include_recordings else []:
        frames = match["frames"]
        pending = [f for f in frames if f["status"] == "pending"]
        recordings.append({
            "id": match["id"],
            "title": match["title"],
            "total": len(frames),
            "pending": len(pending),
            "drafts": sum(f.get("correction") is not None for f in pending),
            "model_drafts": sum(
                model_drafts.get((match["id"], f["id"])) == frame_version(f)
                for f in pending
            ),
            "needs_proposal": sum(
                f.get("proposal") is None
                and f.get("correction") is None
                and model_drafts.get((match["id"], f["id"])) != frame_version(f)
                for f in pending
            ),
            "approved": sum(f["status"] == "approved" for f in frames),
            "skipped": sum(f["status"] == "skipped" for f in frames),
            "usable": 0
            if match.get("synthetic")
            else sum(eligible(f, "people") for f in frames),
            "split": mapping.get(match.get("split_group", match["id"]), "pool"),
        })
    root = store.root / "vision"
    heartbeat = read_record(root / "remote/controller.json") or {}
    checked = heartbeat.get("checked_at")
    fresh = isinstance(checked, int | float) and 0 <= now - checked < HEARTBEAT_MAX_AGE
    jobs = []
    unreadable = 0
    for kind, pattern in (("cloud", "remote/*/job.json"), ("local", "runs/*/run.json")):
        for path in root.glob(pattern):
            record = read_record(path)
            if record is None:
                unreadable += 1
                continue
            jobs.append({
                "id": record.get("id", path.parent.name),
                "source": kind,
                "kind": record.get("kind", "train"),
                "status": record.get("status", "unknown"),
                "snapshot": record.get("snapshot"),
                "created_at": record.get("created_at"),
                "finished_at": record.get("finished_at"),
                "attention": bool(
                    record.get("controller_error") or record.get("error")
                ),
                "cancel_requested": bool(record.get("cancel_requested")),
            })
    return {
        "recordings": recordings,
        "jobs": jobs,
        "unreadable_jobs": unreadable,
        "controller": {
            "fresh": fresh,
            "checked_at": checked,
            "healthy": heartbeat.get("healthy") is True if fresh else None,
            "enabled": heartbeat.get("enabled") is True if fresh else None,
            "cloud_configured": heartbeat.get("cloud_configured") is True
            if fresh
            else None,
        },
    }
