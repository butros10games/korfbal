from django.db import models
from django.urls import reverse

from uuidv7 import uuid7

team_model_string = 'team.Team'

class Season(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    
    def __str__(self):
        return str(self.name)

class Match(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    home_team = models.ForeignKey(team_model_string, on_delete=models.CASCADE, related_name='home_matches')
    away_team = models.ForeignKey(team_model_string, on_delete=models.CASCADE, related_name='away_matches')
    season = models.ForeignKey('Season', on_delete=models.CASCADE, related_name='matches')
    start_time = models.DateTimeField()
        
    def __str__(self):
        return str(self.home_team.name + " - " + self.away_team.name)
    
    def get_absolute_url(self):
        return reverse('match_detail', kwargs={'match_id': self.id_uuid})