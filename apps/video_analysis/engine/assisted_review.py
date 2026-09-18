"""Explicitly import visually checked AI labels without overwriting user reviews."""

from datetime import UTC, datetime
import json
from typing import Any

from .store import ConflictError, Store, frame_version, validate_annotation
from .vision import digest


def import_reviews(store: Store, report: dict[str, Any]) -> int:
    """Apply a complete, version-bound operator report with honest AI provenance.

    Raises:
        ValueError: The report is invalid, duplicated, or contains reviewed frames.
        ConflictError: An input changed after visual inspection.

    """
    provenance = report["provenance"]
    if (
        provenance.get("kind") != "ai"
        or not provenance.get("model")
        or not provenance.get("method")
        or not report.get("frames")
    ):
        raise ValueError("Supply AI model/method provenance and reviewed frames")
    with store.transaction():
        data = store.read()
        current = {
            (match["id"], frame["id"]): (match, frame)
            for match in data["matches"]
            for frame in match["frames"]
        }
        prepared = []
        seen = set()
        for row in report["frames"]:
            identity = (row["match_id"], row["frame_id"])
            if identity in seen or identity not in current:
                raise ValueError("Duplicate, missing or removed review frame")
            seen.add(identity)
            match, frame = current[identity]
            if (
                frame["status"] != "pending"
                or frame.get("correction") is not None
                or frame.get("history")
            ):
                raise ValueError("AI review cannot replace an existing review")
            if (
                frame_version(frame) != row["frame_version"]
                or digest(store.media(frame["image"])) != row["image_sha256"]
            ):
                raise ConflictError("Frame or image changed after inspection")
            annotation = validate_annotation(row["annotation"])
            annotation["notes"] = "AI-reviewed; " + annotation["notes"]
            annotation = validate_annotation(annotation)
            prepared.append((match, frame, annotation))
        for match, frame, annotation in prepared:
            store._review(
                match,
                {
                    "frame_id": frame["id"],
                    "status": "approved",
                    "complete": True,
                    "annotation": annotation,
                },
            )
            frame["annotation_provenance"] = {
                **json.loads(json.dumps(provenance)),
                "reviewed_at": datetime.now(UTC).isoformat(),
            }
        store._persist(data)
    return len(prepared)
