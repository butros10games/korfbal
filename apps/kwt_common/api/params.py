"""Generic API path and query parameter parsing shared by Korfbal apps."""

from __future__ import annotations

from uuid import UUID

from rest_framework.exceptions import ValidationError
from rest_framework.request import Request


UUID_URL_REGEX = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off"})


def uuid_query_value(value: str, *, parameter: str) -> UUID:
    """Parse a UUID query value or raise a controlled API error.

    Raises:
        ValidationError: If the supplied value is not a UUID.

    """
    try:
        return UUID(value)
    except (AttributeError, ValueError):
        raise ValidationError({parameter: "Must be a valid UUID."}) from None


def uuid_query_values(values: list[str], *, parameter: str) -> list[UUID]:
    """Parse repeated UUID query values or raise a controlled API error.

    Raises:
        ValidationError: If any supplied value is not a UUID.

    """
    try:
        return [UUID(value) for value in values]
    except (AttributeError, ValueError):
        raise ValidationError({parameter: "Must be a valid UUID."}) from None


def bool_query_param(request: Request, name: str, *, default: bool) -> bool:
    """Parse a lenient boolean query flag, falling back to ``default``."""
    raw = request.query_params.get(name)
    if not raw:
        return default
    normalized = raw.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default
