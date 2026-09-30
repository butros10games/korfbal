"""Module contains the Match model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models
from django.db.models import Q

from .constants import team_model_string


if TYPE_CHECKING:
    from django.db.models.fields.related_descriptors import RelatedManager

    from apps.awards.models import MatchMvp, MatchMvpVote
    from apps.game_tracker.models import MatchData


class Match(models.Model):
    """Model for Match."""

    # Reverse relations (declared on other models). These are runtime attributes
    # added by Django; we declare them for static type checking.
    if TYPE_CHECKING:
        mvp: MatchMvp
        mvp_votes: RelatedManager[MatchMvpVote]
        tracker_data: MatchData

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    home_team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="home_matches",
    )
    season_id: UUID
    home_team_id: str
    away_team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="away_matches",
    )
    away_team_id: str
    season: models.ForeignKey[Any, Any] = models.ForeignKey(
        "Season",
        on_delete=models.CASCADE,
        related_name="matches",
    )
    pool: models.ForeignKey[Any, Any] = models.ForeignKey(
        "SeasonPool",
        on_delete=models.SET_NULL,
        related_name="matches",
        null=True,
        blank=True,
    )
    pool_id: str | None
    start_time: models.DateTimeField[datetime, datetime] = models.DateTimeField()

    class Meta:
        """Meta class for Match model."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=~Q(home_team=models.F("away_team")),
                name="schedule_match_distinct_teams",
            )
        ]
        indexes: ClassVar[list[Any]] = [
            models.Index(fields=["start_time"]),
            models.Index(fields=["season", "start_time"]),
            models.Index(fields=["pool", "start_time"]),
        ]

    def __str__(self) -> str:
        """Get the string representation of the match.

        Returns:
            str: The names of the home and away teams.

        """
        return str(self.home_team.name + " - " + self.away_team.name)

    def get_absolute_url(self) -> str:
        """Get the absolute URL for the match detail view.

        Returns:
            str: The URL to the match detail view.

        """
        # The legacy Django-rendered `match_detail` route was removed when the
        # project migrated to a React SPA. Match links should point into the SPA.
        return f"{settings.WEB_APP_ORIGIN}/matches/{self.id_uuid}"
