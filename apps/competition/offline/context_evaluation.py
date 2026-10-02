"""Context coverage and deterministic held-out-edition splits for exports.

Pure functions over schema 3 export rows (``export_score_forecasts
--with-context``). They report how much of an export carries explicit
context and split rows by edition so an evaluation never trains on the
season it scores. Nothing here fits or launches a model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any


CONTEXT_FIELDS = (
    "edition",
    "discipline",
    "period",
    "playing_format",
    "category",
    "gender",
)
UNKNOWN = "unknown"


def context_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a row's context with unknown values made explicit."""
    return tuple(
        str(row.get(field)) if row.get(field) not in {None, "", UNKNOWN} else UNKNOWN
        for field in CONTEXT_FIELDS
    )


def context_coverage(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Count rows per context and per unknown field.

    Returns:
        Totals, unknown counts per field and row counts per full context.

    """
    contexts: Counter[tuple[str, ...]] = Counter()
    unknown: Counter[str] = Counter()
    total = 0
    for row in rows:
        total += 1
        key = context_key(row)
        contexts[key] += 1
        for field, value in zip(CONTEXT_FIELDS, key, strict=True):
            if value == UNKNOWN:
                unknown[field] += 1
    return {
        "rows": total,
        "unknown": {field: unknown[field] for field in CONTEXT_FIELDS},
        "contexts": [
            {**dict(zip(CONTEXT_FIELDS, key, strict=True)), "rows": count}
            for key, count in sorted(contexts.items())
        ],
    }


def held_out_split(
    rows: Iterable[Mapping[str, Any]], held_out: set[int]
) -> dict[str, list[Mapping[str, Any]]]:
    """Split rows by edition; rows without an edition are never evaluated.

    Earlier editions train; held-out editions test; later editions are excluded
    so no evaluation uses results from after the season it scores.

    Returns:
        ``train``, ``test`` and ``excluded`` row lists.

    Raises:
        ValueError: No held-out edition was given.

    """
    if not held_out:
        raise ValueError("Choose at least one held-out edition")
    first = min(held_out)
    split: dict[str, list[Mapping[str, Any]]] = {
        "train": [],
        "test": [],
        "excluded": [],
    }
    for row in rows:
        edition = row.get("edition")
        if not isinstance(edition, int):
            split["excluded"].append(row)
        elif edition in held_out:
            split["test"].append(row)
        elif edition < first:
            split["train"].append(row)
        else:
            split["excluded"].append(row)
    return split
