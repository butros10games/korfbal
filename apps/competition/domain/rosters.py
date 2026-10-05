"""Provider roster observation rules shared by publication and team reads."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.utils import timezone

from apps.schedule.domain.competition_context import FULL_SEASON


# A provider roster observation older than this no longer proves membership.
ROSTER_FRESHNESS = timedelta(days=8)
ROSTER_REFRESH_INTERVAL = timedelta(days=7)


class RosterPayloadError(ValueError):
    """A static diagnostic code for a rejected, unmodified person collection."""

    def __init__(self, code: str) -> None:
        """Retain only the violated contract, never a person or provider value."""
        self.code = code
        super().__init__(f"Invalid roster {code}")


@dataclass(frozen=True, slots=True)
class RosterPeriod:
    """The minimum dated context needed to route a roster observation."""

    team_data_id: int
    start_date: date
    end_date: date
    phase: str
    order: int


def roster_target(
    observed_at: datetime, periods: Iterable[RosterPeriod], fallback: int | None
) -> int | None:
    """Choose the running competition period without reaching into history."""
    day = timezone.localdate(observed_at)
    running = [
        period for period in periods if period.start_date <= day <= period.end_date
    ]
    if not running:
        return fallback
    return min(
        running,
        key=lambda period: (
            period.phase == FULL_SEASON,
            period.start_date,
            period.order,
        ),
    ).team_data_id
