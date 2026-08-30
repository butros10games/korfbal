"""Typed fixture builders for awards tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from django.contrib.auth.models import User

from apps.game_tracker.models import MatchPlayer
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import Player
from apps.team.models import Team


@dataclass(frozen=True, slots=True)
class AwardsScenario:
    """Minimal match fixture with helpers for distinct players and roster entries."""

    tracker: TrackerMatchContext
    prefix: str

    @classmethod
    def create(cls, *, finished: bool = True) -> AwardsScenario:
        """Create a match, its tracker aggregate, and optionally finish it."""
        prefix = str(uuid4())
        tracker = create_tracker_match(prefix=prefix)
        if finished:
            tracker.match_data.status = "finished"
            tracker.match_data.save(update_fields=["status"])
        return cls(tracker=tracker, prefix=prefix)

    def player(
        self,
        label: str,
        *,
        team: Team | None = None,
        first_name: str = "",
        last_name: str = "",
    ) -> Player:
        """Create a uniquely named player and optionally put them on the roster."""
        player = create_tracker_player(username=f"{self.prefix}-{label}")
        if first_name or last_name:
            user = cast(User, player.user)
            user.first_name = first_name
            user.last_name = last_name
            user.save(update_fields=["first_name", "last_name"])
        if team is not None:
            MatchPlayer.objects.create(
                match_data=self.tracker.match_data,
                team=team,
                player=player,
            )
        return player
