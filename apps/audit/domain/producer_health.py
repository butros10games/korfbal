"""Pure producer-risk scoring for audit health reports."""

from __future__ import annotations

from datetime import datetime


def _coerce_count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _last_seen_hours(
    *,
    last_seen: object,
    now: datetime,
    window_hours: int,
) -> float:
    if isinstance(last_seen, datetime):
        return max(
            0.0,
            (now - last_seen).total_seconds() / 3600,
        )

    return float(window_hours)


def producer_health_item(
    *,
    row: dict[str, object],
    previous_row: dict[str, object] | None,
    now: datetime,
    window_hours: int,
) -> dict[str, object]:
    """Score one producer from current and previous aggregate metrics."""
    source = str(row["source_system"])
    total = _coerce_count(row["total"])
    errors = _coerce_count(row["errors"])
    warnings = _coerce_count(row["warnings"])
    last_seen = row["last_seen"]

    previous_total = _coerce_count(previous_row["total"]) if previous_row else 0
    previous_errors = _coerce_count(previous_row["errors"]) if previous_row else 0

    current_error_rate = (errors / total) if total else 0.0
    previous_error_rate = (previous_errors / previous_total) if previous_total else 0.0
    error_rate_delta = current_error_rate - previous_error_rate

    last_seen_hours = _last_seen_hours(
        last_seen=last_seen,
        now=now,
        window_hours=window_hours,
    )
    staleness_ratio = min(last_seen_hours, 24.0) / 24.0
    recency_factor = 1.0 - staleness_ratio
    warning_rate = (warnings / total) if total else 0.0
    normalized_volume = min(total, 100) / 100

    risk_score = (
        current_error_rate * 60
        + max(0.0, error_rate_delta) * 20
        + normalized_volume * 10
        + recency_factor * 5
        + warning_rate * 5
    ) * 100

    return {
        "source_system": source,
        "score": round(risk_score, 3),
        "factors": {
            "current_error_rate": round(current_error_rate * 100, 3),
            "previous_error_rate": round(previous_error_rate * 100, 3),
            "error_rate_delta": round(error_rate_delta * 100, 3),
            "warning_rate": round(warning_rate * 100, 3),
            "normalized_volume": round(normalized_volume, 3),
            "last_seen_hours": round(last_seen_hours, 3),
        },
        "totals": {
            "current": {
                "total": total,
                "errors": errors,
                "warnings": warnings,
            },
            "previous": {
                "total": previous_total,
                "errors": previous_errors,
            },
        },
        "last_seen": (
            last_seen.isoformat() if isinstance(last_seen, datetime) else None
        ),
    }
