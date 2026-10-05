"""Retain only verified nonpersonal provider context with field observations."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import math
from typing import Any, Literal, TypedDict, cast

from django.utils import timezone
from django.utils.dateparse import parse_datetime


ContextKind = Literal["team", "pool", "match"]
VERSION = 1
MAX_CLASSES = 64


class SourceClass(TypedDict):
    """Public identity and label of one provider class association."""

    id: str
    name: str | None


type ContextValue = str | bool | int | float | list[SourceClass] | None


TEXT_FIELDS: dict[ContextKind, dict[str, int]] = {
    "team": {"Gender": 80, "TeamCode": 80, "SportDescription": 255, "SportTag": 80},
    "pool": {"CompetitionKind": 80},
    "match": {},
}
FIELDS: dict[ContextKind, set[str]] = {
    "team": {*TEXT_FIELDS["team"], "Class", "SortOrder", "LocalTeam"},
    "pool": {*TEXT_FIELDS["pool"], "SortOrder"},
    "match": {"RoundNr", "ExternalMatchId"},
}


def _text(value: object, field: str, limit: int) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"Invalid source context {field}")
    return value if value.strip() else None


def _classes(value: object) -> list[SourceClass] | None:
    if not isinstance(value, list) or len(value) > MAX_CLASSES:
        raise ValueError("Invalid source context Class")
    classes: dict[str, SourceClass] = {}
    for row in value:
        if not isinstance(row, dict) or not row:
            raise ValueError("Invalid source context Class")
        source_id = _text(row.get("ClassId"), "ClassId", 80)
        name = row.get("ClassName")
        name = _text(name, "ClassName", 255) if name is not None else None
        if source_id is None:
            raise ValueError("Invalid source context ClassId")
        previous = classes.get(source_id)
        if previous and previous["name"] and name and previous["name"] != name:
            raise ValueError("Conflicting source context ClassId")
        classes[source_id] = {
            "id": source_id,
            "name": name or (previous["name"] if previous else None),
        }
    return list(classes.values()) or None


def _value(kind: ContextKind, key: str, value: object) -> ContextValue:
    if value is None:
        return None
    if key in TEXT_FIELDS[kind]:
        return _text(value, key, TEXT_FIELDS[kind][key])
    if key == "LocalTeam":
        if not isinstance(value, bool):
            raise ValueError("Invalid source context LocalTeam")
        return value
    if key == "SortOrder":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or abs(value) > 2**53
            or not math.isfinite(value)
        ):
            raise ValueError("Invalid source context SortOrder")
        return value
    if key in {"RoundNr", "ExternalMatchId"}:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Invalid source context {key}")
        return value
    if key == "Class":
        return _classes(value)
    raise ValueError(f"Unsupported source context {key}")


def context_value(context: object, key: str) -> ContextValue:
    """Read one retained field without interpreting provider designations."""
    if not isinstance(context, dict) or context.get("version") != VERSION:
        return None
    fields = context.get("fields")
    row = fields.get(key) if isinstance(fields, dict) else None
    if not isinstance(row, dict):
        return None
    for kind, keys in FIELDS.items():
        if key not in keys:
            continue
        value = row.get("value")
        if key == "Class" and isinstance(value, list):
            if any(not isinstance(item, dict) for item in value):
                return None
            value = [
                {"ClassId": item.get("id"), "ClassName": item.get("name")}
                for item in value
            ]
        try:
            return _value(kind, key, value)
        except ValueError:
            return None
    return None


def merge_duplicate_context(
    kind: ContextKind, first: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    """Enrich otherwise identical copied provider rows before wrapper deduplication.

    Raises:
        ValueError: A retained field is invalid or conflicts with its duplicate.

    """
    merged = deepcopy(dict(first))
    for key in sorted(FIELDS[kind]):
        previous = _value(kind, key, first.get(key))
        value = _value(kind, key, incoming.get(key))
        if value is None:
            continue
        if previous is None:
            merged[key] = deepcopy(incoming[key])
        elif key == "Class":
            previous = cast("list[SourceClass]", previous)
            value = cast("list[SourceClass]", value)
            combined = {row["id"]: row for row in previous}
            for row in value:
                old = combined.get(row["id"], {})
                if old.get("name") and row["name"] and old["name"] != row["name"]:
                    raise ValueError("Conflicting duplicate source context Class")
                combined[row["id"]] = {
                    "id": row["id"],
                    "name": row["name"] or old.get("name"),
                }
            merged[key] = [
                {"ClassId": row["id"], "ClassName": row["name"]}
                for row in combined.values()
            ]
        elif previous != value:
            raise ValueError(f"Conflicting duplicate source context {key}")
    return merged


def merge_source_context(
    kind: ContextKind,
    existing: object,
    data: Mapping[str, object],
    observed_at: datetime,
    source: str,
) -> dict[str, Any]:
    """Merge supplied allowlisted fields; missing/blank/older rows retain richness.

    A Class association is not a native classification key. Its bounded public
    projection excludes all unverified extra fields and personal attributes.

    Raises:
        ValueError: An observation timestamp or retained field is invalid.

    """
    if timezone.is_naive(observed_at):
        raise ValueError("Source context observation requires a timezone")
    fields = (
        {
            key: dict(row)
            for key, row in existing["fields"].items()
            if key in FIELDS[kind]
            and isinstance(row, dict)
            and context_value(existing, key) is not None
        }
        if isinstance(existing, dict)
        and existing.get("version") == VERSION
        and isinstance(existing.get("fields"), dict)
        else {}
    )
    for key in sorted(FIELDS[kind]):
        if key not in data or (value := _value(kind, key, data[key])) is None:
            continue
        previous = fields.get(key)
        stamp = (
            parse_datetime(previous["observed_at"])
            if isinstance(previous, dict)
            and isinstance(previous.get("observed_at"), str)
            else None
        )
        if stamp is not None and not timezone.is_naive(stamp) and stamp > observed_at:
            continue
        # Repeated abbreviated rows may carry fewer class associations. Without
        # an authoritative replacement contract their omission is not retirement.
        if key == "Class" and isinstance(previous, dict):
            value = cast("list[SourceClass]", value)
            old_classes = previous.get("value")
            if isinstance(old_classes, list):
                combined = {row["id"]: dict(row) for row in old_classes}
                for row in value:
                    old = combined.get(row["id"], {})
                    combined[row["id"]] = {
                        "id": row["id"],
                        "name": row["name"] or old.get("name"),
                    }
                value = list(combined.values())
        # A newer equal value is still ordering evidence. Retaining the older
        # timestamp would let a delayed intermediate observation replace it.
        fields[key] = {
            "value": value,
            "observed_at": observed_at.isoformat(),
            "source": source,
        }
    return {"version": VERSION, "fields": fields} if fields else {}
