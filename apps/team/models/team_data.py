from typing import TYPE_CHECKING
from django.db import models

from .constants import player_model_string

if TYPE_CHECKING:
    from .models import Team
    from schedule.models import Season
    from django.db.models import QuerySet


class TeamData(models.Model):
    team  = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="team_data")
    coach = models.ManyToManyField(
        player_model_string, related_name="team_data_as_coach", blank=True
    )
    players = models.ManyToManyField(
        player_model_string, related_name="team_data_as_player", blank=True
    )
    season = models.ForeignKey(
        "schedule.Season", on_delete=models.CASCADE, related_name="team_data"
    )
    competition = models.CharField(max_length=255, blank=True)

    def __str__(self) -> str:
        return str(self.team.name)
