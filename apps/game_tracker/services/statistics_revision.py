"""Reject computed projections whose match inputs changed during calculation."""

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import transaction

from apps.game_tracker.models import MatchData


class StatisticsRevisionChangedError(RuntimeError):
    """A background task must retry against the latest match state."""


def load_statistics_match(match_data: MatchData) -> MatchData:
    """Discard caller-side relation caches before reading computation inputs."""
    return MatchData.objects.select_related("match_link").get(pk=match_data.pk)


@contextmanager
def publish_statistics_revision(match_data: MatchData) -> Iterator[None]:
    """Check the input revision while serializing publication with tracker writes.

    Raises:
        StatisticsRevisionChangedError: The computation must be retried.

    """
    with transaction.atomic():
        current = MatchData.objects.select_for_update().get(pk=match_data.pk)
        if current.live_revision != match_data.live_revision:
            raise StatisticsRevisionChangedError(
                "Match changed during statistics calculation"
            )
        yield
