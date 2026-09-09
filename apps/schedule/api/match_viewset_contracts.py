"""Minimal viewset context used by schedule action mixins."""

from typing import Protocol

from apps.game_tracker.models import MatchData
from apps.schedule.models import Match


class MatchViewSetContext(Protocol):
    """Object lookup and eagerly loaded tracker data supplied by MatchViewSet."""

    def get_object(self) -> Match:
        """Resolve the requested match using the viewset's filters and permissions."""
        ...

    def _match_data(self, match: Match) -> MatchData | None: ...
