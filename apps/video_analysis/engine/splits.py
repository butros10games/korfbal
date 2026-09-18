"""Stable match-level split assignment shared by exports."""

import hashlib


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
