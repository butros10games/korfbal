from django.db import models
from django.urls import reverse
from uuidv7 import uuid7

player_model_string = 'player.Player'

class Team(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255)
    club = models.ForeignKey('club.Club', on_delete=models.CASCADE, related_name='teams')
    
    def __str__(self):
        return str(self.club.name) + " " + str(self.name)
    
    def get_absolute_url(self):
        return reverse('team_detail', kwargs={'team_id': self.id_uuid})


class TeamData(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='team_data')
    coach = models.ManyToManyField(player_model_string, related_name='team_data_as_coach', blank=True)
    players = models.ManyToManyField(player_model_string, related_name='team_data_as_player', blank=True)
    season = models.ForeignKey('schedule.Season', on_delete=models.CASCADE, related_name='team_data')
    competition = models.CharField(max_length=255, blank=True)
    
    def __str__(self):
        return str(self.team.name)
