"""Module contains the Match model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from bg_uuidv7 import uuidv7
from django.db import models
from django.urls import reverse

from apps.game_tracker.models import MatchData, Shot

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
        return reverse("match_detail", kwargs={"match_id": self.id_uuid})

    def get_final_score(self) -> tuple[int, int]:
        """Compute the final score based on recorded shots.

        Relies on `game_tracker.Shot` records tied to this match's `MatchData`
        entries instead of any denormalized score fragments on `MatchData`.

        Returns:
            tuple[int, int]: (home_score, away_score)

        """
        match_data_ids = list(
            MatchData.objects.filter(match_link=self).values_list("id_uuid", flat=True)
        )
        if not match_data_ids:
            return (0, 0)

        home_score = Shot.objects.filter(
            match_data_id__in=match_data_ids, team=self.home_team, scored=True
        ).count()
        away_score = Shot.objects.filter(
            match_data_id__in=match_data_ids, team=self.away_team, scored=True
        ).count()

        return (home_score, away_score)
