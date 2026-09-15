"""Assertions for additive API errors without weakening existing field contracts."""

from typing import Any


def assert_api_error(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """Require the common error fields and preserve every endpoint-specific field."""
    assert set(actual) == set(expected) | {"code", "message", "detail"}
    assert isinstance(actual["code"], str)
    assert actual["code"]
    assert isinstance(actual["message"], str)
    assert actual["message"]
    for key, value in expected.items():
        assert actual[key] == value
