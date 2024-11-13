from django.db import models

from uuidv7 import uuid7


class MatchData(models.Model):
    STATUS_CHOICES = [
        ('upcoming', 'Upcoming'),
        ('active', 'Active'),
        ('finished', 'Finished'),
    ]
    
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match_link = models.ForeignKey('schedule.Match', on_delete=models.CASCADE)
    home_score = models.IntegerField(default=0)
    away_score = models.IntegerField(default=0)
    parts = models.IntegerField(default=2)
    current_part = models.IntegerField(default=1)
    part_lenght = models.IntegerField(default=1800)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='upcoming')

    def get_winner(self):
        if self.home_score > self.away_score:
            return self.home_team
        elif self.home_score < self.away_score:
            return self.away_team
        else:
            return None
        
    def __str__(self):
        return str(self.match_link.home_team.name + " - " + self.match_link.away_team.name)