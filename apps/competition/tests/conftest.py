"""Shared synthetic competition season."""

from datetime import date

import pytest

from apps.schedule.models import Season


@pytest.fixture
def season() -> Season:
    """Provide explicit source season boundaries."""
    return Season.objects.create(
        name="2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )
