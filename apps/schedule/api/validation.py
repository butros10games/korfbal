"""Validation helpers for schedule API query parameters."""

from __future__ import annotations

from uuid import UUID

from rest_framework.exceptions import ValidationError


UUID_URL_REGEX = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def uuid_query_values(values: list[str], *, parameter: str) -> list[UUID]:
    """Parse repeated UUID query values or raise a controlled API error.

    Raises:
        ValidationError: If any supplied value is not a UUID.

    """
    try:
        return [UUID(value) for value in values]
    except (AttributeError, ValueError):
        raise ValidationError({parameter: "Must be a valid UUID."}) from None


MAX_MATCH_SUMMARY_LIMIT = 200


def match_summary_limit(value: str | None, *, default: int) -> int:
    """Bound summary queries while retaining legacy default/minimum behavior.

    Raises:
        ValidationError: The requested result count exceeds the supported maximum.

    """
    try:
        limit = int(value) if value else default
    except ValueError:
        return default
    if limit > MAX_MATCH_SUMMARY_LIMIT:
        raise ValidationError({"limit": f"Must be at most {MAX_MATCH_SUMMARY_LIMIT}."})
    return max(limit, 1)


def match_summary_offset(value: str | None) -> int:
    """Parse an optional nonnegative offset within the database integer range.

    Raises:
        ValidationError: The offset is not a supported nonnegative integer.

    """
    try:
        offset = int(value) if value is not None else 0
    except ValueError:
        offset = -1
    if not 0 <= offset <= 2**31 - 1:
        raise ValidationError({
            "offset": "Must be an integer between 0 and 2147483647."
        })
    return offset
