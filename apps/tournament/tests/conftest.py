"""Every tournament test also runs on PostgreSQL (planning, locks, live writes)."""

from pathlib import Path

import pytest


HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark this directory's tests for the PostgreSQL parity lane."""
    for item in items:
        if HERE in Path(item.path).parents:
            item.add_marker(pytest.mark.postgres_parity)
