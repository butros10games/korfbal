from django.db import models

from .constants import team_model_string, player_model_string

from uuidv7 import uuid7

    
class MatchPlayer(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match_data = models.ForeignKey("MatchData", on_delete=models.CASCADE, related_name="players")
    team = models.ForeignKey(team_model_string, on_delete=models.CASCADE, related_name="match_players")
    player = models.ForeignKey(player_model_string, on_delete=models.CASCADE, related_name="match_players")

    def __str__(self):
        return str(self.player)
