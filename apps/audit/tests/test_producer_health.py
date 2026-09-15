"""Exact health factors, beyond relative ordering of two API rows."""

from datetime import UTC, datetime, timedelta

import pytest

from apps.audit.domain.producer_health import producer_health_item


NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def test_health_reports_each_factor_and_the_weighted_score() -> None:
    """A worsening producer's volume, recency and warnings all affect its score."""
    seen = NOW - timedelta(hours=6)
    assert producer_health_item(
        row={
            "source_system": "synthetic",
            "total": 100,
            "errors": 20,
            "warnings": 10,
            "last_seen": seen,
        },
        previous_row={"total": 100, "errors": 10},
        now=NOW,
        window_hours=48,
    ) == {
        "source_system": "synthetic",
        "score": 2825.0,
        "factors": {
            "current_error_rate": 20.0,
            "previous_error_rate": 10.0,
            "error_rate_delta": 10.0,
            "warning_rate": 10.0,
            "normalized_volume": 1.0,
            "last_seen_hours": 6.0,
        },
        "totals": {
            "current": {"total": 100, "errors": 20, "warnings": 10},
            "previous": {"total": 100, "errors": 10},
        },
        "last_seen": seen.isoformat(),
    }


@pytest.mark.parametrize(
    ("counts", "previous", "hours", "score"),
    [
        ((10, 0, 2), None, 0, 700.0),
        ((10, 0, 2), None, -6, 700.0),
        ((100, 10, 0), {"total": 100, "errors": 20}, 0, 2100.0),
        ((200, 20, 10), {"total": 100, "errors": 10}, 48, 1625.0),
        ((0, 0, 0), {"total": 0, "errors": 0}, 24, 0.0),
    ],
)
def test_health_caps_volume_and_recency_without_rewarding_falling_error_rates(
    counts: tuple[int, int, int],
    previous: dict[str, int] | None,
    hours: int,
    score: float,
) -> None:
    """Future clocks, large producers and improved error rates stay bounded."""
    total, errors, warnings = counts
    result = producer_health_item(
        row={
            "source_system": "synthetic",
            "total": total,
            "errors": errors,
            "warnings": warnings,
            "last_seen": NOW - timedelta(hours=hours),
        },
        previous_row=previous,
        now=NOW,
        window_hours=48,
    )
    assert result["score"] == score


@pytest.mark.parametrize("missing", [None, "not-a-timestamp"])
def test_missing_activity_uses_the_requested_window(missing: object) -> None:
    """Missing timestamps must not masquerade as a just-active producer."""
    result = producer_health_item(
        row={
            "source_system": "synthetic",
            "total": 0,
            "errors": 0,
            "warnings": 0,
            "last_seen": missing,
        },
        previous_row=None,
        now=NOW,
        window_hours=12,
    )
    expected_score = 250.0
    assert result["score"] == expected_score
    assert result["last_seen"] is None
    assert result["totals"] == {
        "current": {"total": 0, "errors": 0, "warnings": 0},
        "previous": {"total": 0, "errors": 0},
    }
