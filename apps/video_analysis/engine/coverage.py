"""CPU-only frozen dataset validation and annotation coverage reports."""

from __future__ import annotations

from collections import Counter
import math
from typing import Any

from .store import Store
from .vision import artifact, verify_snapshot


LABEL_FIELDS = 5
ROUNDING_TOLERANCE = 1e-7


def label_rows(text: str, classes: list[str]) -> list[int]:
    """Validate normalized YOLO boxes and return their class indices.

    Raises:
        ValueError: If a row has an invalid class, coordinate or extent.

    """
    result = []
    for row in text.splitlines():
        if not row.strip():
            continue
        fields = row.split()
        if len(fields) != LABEL_FIELDS:
            raise ValueError("Each label must contain a class and four coordinates")
        index = int(fields[0])
        x, y, width, height = map(float, fields[1:])
        if (
            not 0 <= index < len(classes)
            or not all(math.isfinite(v) for v in (x, y, width, height))
            or min(width, height) <= 0
            or min(x - width / 2, y - height / 2) < -ROUNDING_TOLERANCE
            or max(x + width / 2, y + height / 2) > 1 + ROUNDING_TOLERANCE
        ):
            raise ValueError("Label class or box lies outside the image")
        result.append(index)
    return result


def dataset_report(store: Store, snapshot: str) -> dict[str, Any]:
    """Check exported labels and report coverage without changing the dataset.

    Raises:
        ValueError: If labels, split boundaries or manifest counts are invalid.

    """
    root = artifact(store, "snapshots", snapshot)
    manifest = verify_snapshot(root)
    classes = manifest["classes"]
    counts: Counter[str] = Counter()
    groups: dict[str, str] = {}
    media: dict[tuple[str, str], str] = {}
    summaries: dict[str, Any] = {}
    for record in manifest["frames"]:
        split = record["split"]
        if split not in {"train", "val", "test"}:
            raise ValueError("Unknown dataset split")
        group = record["group"]
        if groups.setdefault(group, split) != split:
            raise ValueError("A match group crosses dataset splits")
        for kind in ("image_sha256", "video_sha256"):
            value = record.get(kind)
            if value and media.setdefault((kind, value), split) != split:
                raise ValueError("Source media crosses dataset splits")
        indices = label_rows((root / record["label"]).read_text(), classes)
        counts[split] += 1
        summary = summaries.setdefault(
            split,
            {
                "frames": 0,
                "empty_frames": 0,
                "groups": {},
                "classes": {name: {"objects": 0, "frames": 0} for name in classes},
            },
        )
        summary["frames"] += 1
        summary["empty_frames"] += not indices
        summary["groups"][group] = summary["groups"].get(group, 0) + 1
        for index, total in Counter(indices).items():
            summary["classes"][classes[index]]["objects"] += total
            summary["classes"][classes[index]]["frames"] += 1
    if dict(counts) != manifest["counts"] or groups != manifest["splits"]:
        raise ValueError("Manifest counts or split groups disagree with its frames")
    if not counts["train"] or not counts["val"]:
        raise ValueError("Training and validation each need labeled frames")
    return {
        "snapshot": snapshot,
        "integrity": "passed",
        "splits": summaries,
        "warnings": coverage_warnings(summaries),
        "note": (
            "Coverage is not model accuracy. Empty frames are allowed negatives; "
            "confirm that they contain no target objects. Missing classes and few "
            "matches limit what an experiment can teach us."
        ),
    }


def coverage_warnings(summaries: dict[str, Any]) -> list[str]:
    """Describe absent classes and limited match diversity without blocking pilots."""
    warnings = []
    for split, summary in summaries.items():
        if len(summary["groups"]) == 1:
            warnings.append(f"{split}: only one match group; limited match diversity")
        for name, totals in summary["classes"].items():
            if not totals["objects"]:
                warnings.append(f"{split}: no {name} labels")
    return warnings
