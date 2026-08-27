"""Regression tests for the public OpenAPI contract."""

from pathlib import Path

from django.core.management import call_command


def test_openapi_schema_has_no_warnings_or_errors(tmp_path: Path) -> None:
    """Keep every API endpoint discoverable and its schema structurally valid."""
    call_command(
        "spectacular",
        validate=True,
        fail_on_warn=True,
        file=str(tmp_path / "openapi.yaml"),
        verbosity=0,
    )
