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
