"""Active-play intervals on the original recording timeline."""

from __future__ import annotations

import math

from .store import number


MAX_PERIODS = 30
MAX_IMAGES = 1000


def validate_periods(raw: object, duration: float) -> list[dict[str, float]]:
    """Require ordered, disjoint intervals wholly inside a recording.

    Raises:
        ValueError: If a period is missing, invalid, or overlaps another.
        TypeError: If a period is not an object.

    """
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PERIODS:
        raise ValueError("Choose between 1 and 30 active-play periods")
    periods: list[dict[str, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("Invalid active-play period")
        start = number(item.get("start"), 0, duration)
        end = number(item.get("end"), 0, duration)
        if end <= start or (periods and start < periods[-1]["end"]):
            raise ValueError("Active-play periods must be ordered and cannot overlap")
        periods.append({"start": start, "end": end})
    return periods


def sample_times(periods: list[dict[str, float]], interval: int) -> list[float]:
    """Sample each active period independently; gaps never yield images.

    Raises:
        ValueError: If the interval or image count is outside policy limits.

    """
    if interval not in {10, 20, 30}:
        raise ValueError("Choose a 10, 20 or 30 second image interval")
    times = [
        round(period["start"] + index * interval, 3)
        for period in periods
        for index in range(math.ceil((period["end"] - period["start"]) / interval))
    ]
    if len(times) > MAX_IMAGES:
        raise ValueError("At most 1000 images can be prepared in one batch")
    return times


def is_active_time(match: dict, time: float) -> bool:
    """Keep legacy recordings open while enforcing saved or required cuts."""
    periods = match.get("active_periods") or []
    if not periods:
        return not match.get("timeline_required")
    return any(period["start"] <= time < period["end"] for period in periods)
