"""Validated, revisioned review records and training export."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
import fcntl
import hashlib
import io
import json
import math
from pathlib import Path
import re
import threading
from typing import Any
import zipfile

from .splits import split_for_group


LABELS = ("ball", "player", "basket", "referee")
TEAMS = ("unknown", "team_a", "team_b")
EXPORT_LABELS = (*LABELS, "player_team_a", "player_team_b")
PROMPT_VERSION = "korfbal-frame-v2"
SCENES = ("live", "replay", "break", "unknown")
EVENTS = ("none", "shot", "goal", "unknown")
STATUSES = ("pending", "approved", "skipped")
BOX_SIZE = 4
BOX_LIMIT = 1.000001
MAX_OBJECTS = 80
MAX_NOTES = 2000
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


class ConflictError(ValueError):
    """A newer review was saved by another browser or process."""


def number(value: object, minimum: float, maximum: float) -> float:
    """Require a finite number within inclusive bounds.

    Raises:
        ValueError: If input data or the requested operation is invalid.

    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"Expected a number between {minimum} and {maximum}")
    return float(value)


def validate_annotation(raw: object) -> dict[str, Any]:
    """Validate both model proposals and human corrections without coercion.

    Raises:
        TypeError: If a request field has an unsupported type.
        ValueError: If input data or the requested operation is invalid.

    """
    if not isinstance(raw, dict):
        raise TypeError("Annotation must be an object")
    if raw.get("scene") not in SCENES or raw.get("event") not in EVENTS:
        raise ValueError("Invalid scene or event")
    clean = validate_objects(raw.get("objects"))
    notes = raw.get("notes", "")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES:
        raise ValueError("Notes must contain at most 2000 characters")
    visibility = raw.get("ball_visibility")
    if visibility is not None and visibility not in {
        "visible",
        "occluded",
        "outside",
        "uncertain",
    }:
        raise ValueError("Invalid ball visibility")
    if visibility in {"occluded", "outside"} and any(
        o["label"] == "ball" for o in clean
    ):
        raise ValueError("Remove the ball box or mark the ball visible/uncertain")
    return {
        **({"ball_visibility": visibility} if visibility is not None else {}),
        "scene": raw["scene"],
        "event": raw["event"],
        "objects": clean,
        "notes": notes,
    }


def validate_objects(objects: object) -> list[dict[str, Any]]:
    """Validate object geometry and identity attributes.

    Raises:
        ValueError: If any object has invalid geometry or attributes.

    """
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise ValueError("Expected at most 80 objects")
    clean = []
    for item in objects:
        if not isinstance(item, dict) or item.get("label") not in LABELS:
            raise ValueError("Invalid object label")
        box = item.get("bbox")
        if not isinstance(box, list) or len(box) != BOX_SIZE:
            raise ValueError("Bounding box must contain x, y, width, height")
        x, y, width, height = [number(v, 0, 1) for v in box]
        if width <= 0 or height <= 0 or x + width > BOX_LIMIT or y + height > BOX_LIMIT:
            raise ValueError("Bounding box must fit inside the image")
        team = item.get("team", "unknown")
        if team not in TEAMS or (item["label"] != "player" and team != "unknown"):
            raise ValueError("Invalid team: only players can belong to a team")
        track = item.get("track_id", "")
        if not isinstance(track, str) or (track and not SAFE_ID.fullmatch(track)):
            raise ValueError("Invalid track ID")
        if "temporal_estimate" in item and not isinstance(
            item["temporal_estimate"], bool
        ):
            raise ValueError("Invalid temporal estimate flag")
        clean.append({
            "label": item["label"],
            "bbox": [x, y, width, height],
            "confidence": number(item.get("confidence"), 0, 1),
            **({"team": team} if "team" in item else {}),
            **({"track_id": track} if track else {}),
            **({"temporal_estimate": True} if item.get("temporal_estimate") else {}),
        })
    return clean


def ball_review_complete(annotation: dict[str, Any]) -> bool:
    """Require an observed ball or an explicit visibility decision for ball training."""
    visibility = annotation.get("ball_visibility")
    if visibility == "uncertain":
        return False
    if visibility in {"outside", "occluded"}:
        return True
    return any(obj["label"] == "ball" for obj in annotation["objects"])


def frame_version(frame: dict[str, Any]) -> str:
    """Fingerprint one frame so unrelated imports do not invalidate its review."""
    content = {
        key: value
        for key, value in frame.items()
        if key not in {"frame_version", "curation"}
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def blank_annotation() -> dict[str, Any]:
    """Return an explicitly unknown frame, never an inferred negative."""
    return {"scene": "unknown", "event": "unknown", "objects": [], "notes": ""}


def atomic_json(path: Path, data: object) -> None:
    """Replace a JSON file atomically, preserving the old file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


class Store:
    """Single-server review store with optimistic concurrency and durable history."""

    def __init__(self, root: Path) -> None:
        """Load a local dataset, creating an empty catalog when necessary."""
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "review.json"
        self.lock = threading.RLock()
        with self.transaction():
            if not self.path.exists():
                atomic_json(
                    self.path, {"schema_version": 1, "revision": 0, "matches": []}
                )

    def read(self) -> dict[str, Any]:
        """Read the latest persisted revision."""
        with self.lock:
            return json.loads(self.path.read_text())

    def recording(self, match_id: str) -> dict[str, Any]:
        """Read only the metadata needed by a video worker.

        Raises:
            ValueError: The recording is unknown.

        """
        match = next((m for m in self.read()["matches"] if m["id"] == match_id), None)
        if match is None:
            raise ValueError("Unknown recording")
        return {k: v for k, v in match.items() if k != "frames"}

    def publish_media(self, relative: str) -> None:
        """Persist one extracted image when using remote storage."""

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize CLI and web writes across threads and processes."""
        with self.lock, (self.root / ".review.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def media(self, relative: str) -> Path:
        """Resolve an existing media file within the dataset root.

        Raises:
            ValueError: If input data or the requested operation is invalid.

        """
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Media file not found")
        return path

    def add_match(self, match: dict[str, Any]) -> None:
        """Append one prepared recording without overwriting existing reviews.

        Raises:
            ValueError: If input data or the requested operation is invalid.

        """
        with self.transaction():
            data = self.read()
            if any(item["id"] == match["id"] for item in data["matches"]):
                raise ValueError("Match already exists; choose a different ID")
            data["matches"].append(match)
            self._persist(data)

    def media_size(self, relative: str) -> int:
        """Read media length without requiring an HTTP caller to open it."""
        return self.media(relative).stat().st_size

    def media_chunks(self, relative: str, start: int, end: int) -> Iterator[bytes]:
        """Stream a bounded byte range from local media.

        Yields:
            Bounded response chunks.

        """
        with self.media(relative).open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    def sync_artifacts(self) -> None:
        """Publish completed files when the persistence adapter uses remote storage."""

    def publish_artifact(self, relative: str) -> None:
        """Publish one changed artifact when using remote storage."""

    def _persist(self, data: dict[str, Any]) -> None:
        if (self.root / ".migrated-to-django").exists():
            raise ConflictError(
                "Workspace moved to KorfConnect. Open the native video analysis page."
            )
        data["revision"] += 1
        atomic_json(self.path, data)

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save a frame review or timing anchor only against the observed revision.

        Raises:
            ConflictError: If another client has saved a newer revision.
            ValueError: If input data or the requested operation is invalid.

        """
        with self.transaction():
            data = self.read()
            scoped_review = (
                payload.get("action") == "review"
                and "expected_frame_version" in payload
            )
            if not scoped_review and payload.get("revision") != data["revision"]:
                raise ConflictError(
                    "Another review was saved. Reload before applying your edit."
                )
            match = next(
                (m for m in data["matches"] if m["id"] == payload.get("match_id")), None
            )
            if match is None:
                raise ValueError("Unknown match")
            if payload.get("action") == "timing":
                self._timing(match, payload)
            elif payload.get("action") == "review":
                if scoped_review:
                    frame = next(
                        (
                            f
                            for f in match["frames"]
                            if f["id"] == payload.get("frame_id")
                        ),
                        None,
                    )
                    if frame is None:
                        raise ValueError("Unknown frame")
                    if payload["expected_frame_version"] != frame_version(frame):
                        raise ConflictError(
                            "Another review was saved for this frame. "
                            "Reload before applying your edit."
                        )
                self._review(match, payload)
            else:
                raise ValueError("Unknown action")
            self._persist(data)
            return data

    @staticmethod
    def _timing(match: dict[str, Any], payload: dict[str, Any]) -> None:
        # The kickoff offset may precede an excerpt on the broadcast timeline.
        value = number(payload.get("start_seconds"), 0, 86400)
        match.setdefault("timing_history", []).append({
            "previous": match.get("match_start_seconds"),
            "value": value,
            "at": datetime.now(UTC).isoformat(),
        })
        match["match_start_seconds"] = value

    @staticmethod
    def _review(match: dict[str, Any], payload: dict[str, Any]) -> None:
        frame = next(
            (f for f in match["frames"] if f["id"] == payload.get("frame_id")), None
        )
        if frame is None or payload.get("status") not in STATUSES:
            raise ValueError("Unknown frame or status")
        annotation = validate_annotation(payload.get("annotation"))
        complete = payload.get("complete")
        if not isinstance(complete, bool):
            raise TypeError("Annotation completeness must be explicit")
        if annotation["event"] in {"shot", "goal"} and annotation["scene"] != "live":
            raise ValueError(
                "Shots and goals must be labeled on live play, not replays"
            )
        if payload["status"] == "approved" and not complete:
            raise ValueError(
                "Check all visible objects before approving a training frame"
            )
        frame.setdefault("history", []).append({
            "status": frame["status"],
            "correction": frame.get("correction"),
            "complete": frame.get("complete", False),
            "annotation_provenance": frame.get("annotation_provenance"),
            "at": datetime.now(UTC).isoformat(),
        })
        frame.pop("annotation_provenance", None)
        frame.update(status=payload["status"], correction=annotation, complete=complete)
        frame["reviewed_at"] = datetime.now(UTC).isoformat()

    def proposal(
        self, match_id: str, frame_id: str, prediction: dict[str, Any]
    ) -> None:
        """Attach model output once; never replace human or previous model work.

        Raises:
            ValueError: If input data or the requested operation is invalid.

        """
        annotation = validate_annotation(prediction)
        with self.transaction():
            data = self.read()
            match = next(m for m in data["matches"] if m["id"] == match_id)
            frame = next(f for f in match["frames"] if f["id"] == frame_id)
            if (
                frame.get("proposal") is not None
                or frame["status"] != "pending"
                or frame.get("correction") is not None
            ):
                raise ValueError("Frame already has a proposal or review")
            frame.update(
                prompt_version=PROMPT_VERSION,
                proposal=annotation,
                model="gpt-5.6-luna",
                proposed_at=datetime.now(UTC).isoformat(),
            )
            self._persist(data)

    def export(self) -> bytes:
        """Export approved complete frames as YOLO labels plus immutable provenance."""
        data = self.read()
        output = io.BytesIO()
        exported = []
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for match in data["matches"]:
                if match.get("synthetic"):
                    continue
                exported.extend(self._export_match(archive, match))
            archive.writestr(
                "annotations.json",
                json.dumps(
                    {
                        "schema_version": 2,
                        "revision": data["revision"],
                        "classes": EXPORT_LABELS,
                        "frames": exported,
                        "event_labels_are_frame_context_only": True,
                        "confidence_is_uncalibrated": True,
                        "split_counts": dict(Counter(f["split"] for f in exported)),
                    },
                    indent=2,
                ),
            )
            archive.writestr(
                "data.yaml",
                "path: .\ntrain: images/train\nval: images/val\ntest: images/test\n"
                "names:\n"
                + "".join(
                    f"  {index}: {label}\n" for index, label in enumerate(EXPORT_LABELS)
                ),
            )
        return output.getvalue()

    def _export_match(
        self, archive: zipfile.ZipFile, match: dict[str, Any]
    ) -> list[dict[str, Any]]:
        exported = []
        split = split_for_group(match.get("split_group", match["id"]))
        # Existing clip planner calls this split 'validation'; YOLO uses 'val'.
        split = "val" if split == "validation" else split
        for frame in match["frames"]:
            if frame["status"] != "approved" or not frame.get("complete"):
                continue
            annotation = validate_annotation(frame["correction"])
            if not ball_review_complete(annotation):
                continue
            name = f"{match['id']}-{frame['id']}"
            image = self.media(frame["image"])
            archive.write(image, f"images/{split}/{name}{image.suffix}")
            lines = []
            for obj in annotation["objects"]:
                x, y, width, height = obj["bbox"]
                label = obj["label"]
                if label == "player" and obj.get("team", "unknown") != "unknown":
                    label = f"player_{obj['team']}"
                lines.append(
                    f"{EXPORT_LABELS.index(label)} {x + width / 2:.6f} "
                    f"{y + height / 2:.6f} {width:.6f} {height:.6f}"
                )
            archive.writestr(f"labels/{split}/{name}.txt", "\n".join(lines))
            record = deepcopy(frame)
            record.update(
                dataset_image=f"images/{split}/{name}{image.suffix}",
                dataset_label=f"labels/{split}/{name}.txt",
                width=match["width"],
                height=match["height"],
                match_id=match["id"],
                match_title=match["title"],
                team_convention=(
                    "team_a is first listed team; team_b is second listed team"
                ),
                team_hints=match.get("team_hints", {}),
                source_url=match.get("source_url", ""),
                split=split,
                video_sha256=match.get("video_sha256"),
                match_start_seconds=match.get("match_start_seconds"),
            )
            exported.append(record)
        return exported
