"""Checked date arithmetic shared by tournament planning services."""

from datetime import UTC, datetime, timedelta


def add_schedule_time(
    start: datetime,
    delta: timedelta,
    *,
    error_type: type[Exception],
) -> datetime:
    """Keep local and persisted UTC schedule times within the supported range.

    Arithmetic failures are translated into the caller's domain error.

    """
    try:
        if start.tzinfo is not None:
            start.astimezone(UTC)
        result = start + delta
        if result.tzinfo is not None:
            result.astimezone(UTC)
    except OverflowError as error:
        raise error_type("The schedule exceeds the supported date range.") from error
    return result
