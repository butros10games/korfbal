from django.db import models
from django.contrib.auth.models import User  # Assuming you're using the built-in User model

import uuid

class Club(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)

class Team(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='teams')
    
class TeamData(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='team_data')
    coach = models.ManyToManyField('Player', related_name='teams')
    players = models.ManyToManyField('Player', related_name='teams')
    Season = models.ForeignKey('Season', on_delete=models.CASCADE, related_name='teams')

class Player(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ManyToManyField(User, related_name='players')

class Match(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    home_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='home_matches')
    away_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='away_matches')
    home_score = models.IntegerField()
    away_score = models.IntegerField()
    start_time = models.DateTimeField()
    length = models.IntegerField()

    def get_winner(self):
        if self.home_score > self.away_score:
            return self.home_team
        elif self.home_score < self.away_score:
            return self.away_team
        else:
            return None

class PlayerGroup(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    players = models.ManyToManyField(Player, related_name='player_groups')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='player_groups')
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='player_groups')
    starting_type = models.CharField(max_length=255)
    current_type = models.CharField(max_length=255)

class PlayerChange(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    player_in = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='player_changes')
    player_out = models.ForeignKey(Player, on_delete=models.CASCADE)
    player_group = models.ForeignKey(PlayerGroup, on_delete=models.CASCADE, related_name='player_changes')
    time = models.IntegerField()

class Goal(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='goals')
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='goals')
    time = models.IntegerField()
    goal_type = models.ForeignKey('GoalType', on_delete=models.CASCADE, related_name='goals')

class GoalType(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)

class Pause(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='pauses')
    time = models.IntegerField()
    active = models.BooleanField()

class Season(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    