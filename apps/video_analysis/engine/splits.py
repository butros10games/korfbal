"""Stable match-level split assignment shared by exports."""

from collections.abc import Mapping
import hashlib
from typing import Any


TRAIN_BOUNDARY = 0.8
VALIDATION_BOUNDARY = 0.9


def split_for_group(group: str) -> str:
    """Keep every frame of a recording in the same dataset partition."""
    bucket = int.from_bytes(hashlib.sha256(group.encode()).digest()[:8], "big") / 2**64
    return (
        "train"
        if bucket < TRAIN_BOUNDARY
        else "validation"
        if bucket < VALIDATION_BOUNDARY
        else "test"
    )


def held_out_season_splits(
    groups: Mapping[str, Mapping[str, Any]], held_out: set[int]
) -> dict[str, str]:
    """Assign whole groups for an explicit held-out-season evaluation.

    Groups from held-out editions are the test set. Other groups with a known
    edition keep their stable hash between train and validation; groups
    without an edition stay in the unassigned pool rather than leaking into
    either side. The default ``split_for_group`` assignment is unchanged.

    Returns:
        Group to split name.

    """
    assigned = {}
    for group, context in groups.items():
        edition = context.get("edition")
        if not isinstance(edition, int):
            assigned[group] = "pool"
        elif edition in held_out:
            assigned[group] = "test"
        else:
            bucket = (
                int.from_bytes(hashlib.sha256(group.encode()).digest()[:8], "big")
                / 2**64
            )
            assigned[group] = (
                "train" if bucket < TRAIN_BOUNDARY / VALIDATION_BOUNDARY else "val"
            )
    return assigned
