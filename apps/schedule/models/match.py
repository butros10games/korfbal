"""Module contains the Match model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models
from django.db.models import Count, Q

from apps.game_tracker.models import Shot

from .constants import team_model_string


if TYPE_CHECKING:
    from .mvp import MatchMvp, MatchMvpVote


class Match(models.Model):
    """Model for Match."""

    # Reverse relations (declared on other models). These are runtime attributes
    # added by Django; we declare them for static type checking.
    if TYPE_CHECKING:
        mvp: MatchMvp
        mvp_votes: models.Manager[MatchMvpVote]

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
    away_team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="away_matches",
    )
    season: models.ForeignKey[Any, Any] = models.ForeignKey(
        "Season",
        on_delete=models.CASCADE,
        related_name="matches",
    )
    start_time: models.DateTimeField[datetime, datetime] = models.DateTimeField()

    class Meta:
        """Meta class for Match model."""

        indexes: ClassVar[list[Any]] = [
            models.Index(fields=["start_time"]),
            models.Index(fields=["home_team"]),
            models.Index(fields=["away_team"]),
            models.Index(fields=["season", "start_time"]),
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

    def get_final_score(self) -> tuple[int, int]:
        """Compute the final score based on recorded shots.

        Relies on `game_tracker.Shot` records tied to this match's `MatchData`
        entries instead of any denormalized score fragments on `MatchData`.

        Returns:
            tuple[int, int]: (home_score, away_score)

        """
        # NOTE: Historically this method performed multiple queries per call.
        # Keep it correct but make it cheap-ish: a single aggregate query.
        totals = Shot.objects.filter(
            match_data__match_link=self,
            scored=True,
            team__isnull=False,
        ).aggregate(
            home=Count("id_uuid", filter=Q(team=self.home_team)),
            away=Count("id_uuid", filter=Q(team=self.away_team)),
        )
        return (int(totals.get("home") or 0), int(totals.get("away") or 0))
