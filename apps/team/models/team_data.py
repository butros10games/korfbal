from django.db import models

from .constants import player_model_string


class TeamData(models.Model):
    team = models.ForeignKey('Team', on_delete=models.CASCADE, related_name='team_data')
    coach = models.ManyToManyField(player_model_string, related_name='team_data_as_coach', blank=True)
    players = models.ManyToManyField(player_model_string, related_name='team_data_as_player', blank=True)
    season = models.ForeignKey('schedule.Season', on_delete=models.CASCADE, related_name='team_data')
    competition = models.CharField(max_length=255, blank=True)
    
    def __str__(self):
        return str(self.team.name)
