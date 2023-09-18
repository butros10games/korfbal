from django.db import models
from django.contrib.auth.models import User  # Assuming you're using the built-in User model

import uuid

class Club(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    
    def __str__(self):
        return str(self.name)

class Team(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='teams')
    
    def __str__(self):
        return str(self.name)
    
class TeamData(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='team_data')
    coach = models.ManyToManyField('Player', related_name='team_data_as_coach', blank=True)
    players = models.ManyToManyField('Player', related_name='team_data_as_player', blank=True)
    season = models.ForeignKey('Season', on_delete=models.CASCADE, related_name='team_data')
    
    def __str__(self):
        return str(self.team.name)

class Player(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='players')
    team_follow = models.ManyToManyField('Team', related_name='Follow', blank=True)
    profile_picture = models.ImageField(upload_to='media/profile_pictures/', blank=True)
    
    def __str__(self):
        return str(self.user.username)

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
    
    def __str__(self):
        return str(self.name)

class Pause(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='pauses')
    time = models.IntegerField()
    active = models.BooleanField(default=True)

class Season(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    
    def __str__(self):
        return str(self.name)
    