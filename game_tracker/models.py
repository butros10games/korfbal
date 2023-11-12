from django.db import models
from django.contrib.auth.models import User  # Assuming you're using the built-in User model
from django.urls import reverse

import uuid

from game_tracker.programs.uuidv7 import uuid7

class Club(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    admin = models.ManyToManyField('Player', related_name='clubs', blank=True)
    logo = models.ImageField(upload_to='static/images/clubs/club_pictures/', default='/static/images/clubs/blank-club-picture.png', blank=True)
    
    def __str__(self):
        return str(self.name)
    
    def get_absolute_url(self):
        return reverse("club_detail", kwargs={"club_id": self.id_uuid})

class Team(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255)
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='teams')
    
    def __str__(self):
        return str(self.club.name)  + " " +  str(self.name)
    
    def get_absolute_url(self):
        return reverse('team_detail', kwargs={'team_id': self.id_uuid})
    
class TeamData(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='team_data')
    coach = models.ManyToManyField('Player', related_name='team_data_as_coach', blank=True)
    players = models.ManyToManyField('Player', related_name='team_data_as_player', blank=True)
    season = models.ForeignKey('Season', on_delete=models.CASCADE, related_name='team_data')
    
    def __str__(self):
        return str(self.team.name)

class Player(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='players')
    team_follow = models.ManyToManyField('Team', related_name='Follow', blank=True)
    club_follow = models.ManyToManyField('Club', related_name='Follow', blank=True)
    profile_picture = models.ImageField(upload_to='static/profile_pictures/', default='/static/images/player/blank-profile-picture.png', blank=True)
    
    def __str__(self):
        return str(self.user.username)
    
    def get_absolute_url(self):
        return reverse('profile_detail', kwargs={'player_id': self.id_uuid})

class Match(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    home_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='home_matches')
    away_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='away_matches')
    home_score = models.IntegerField(default=0)
    away_score = models.IntegerField(default=0)
    start_time = models.DateTimeField()
    parts = models.IntegerField(default=2)
    current_part = models.IntegerField(default=1)
    part_lenght = models.IntegerField(default=1800)
    active = models.BooleanField(default=False)
    finished = models.BooleanField(default=False)

    def get_winner(self):
        if self.home_score > self.away_score:
            return self.home_team
        elif self.home_score < self.away_score:
            return self.away_team
        else:
            return None
        
    def __str__(self):
        return str(self.home_team.name + " - " + self.away_team.name)
    
    def get_absolute_url(self):
        return reverse('match_detail', kwargs={'match_id': self.id_uuid})
    
class MatchPart(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='match_parts')
    part_number = models.IntegerField()
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True, null=True)
    active = models.BooleanField(default=True)

class PlayerGroup(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    players = models.ManyToManyField(Player, related_name='player_groups', blank=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='player_groups')
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='player_groups')
    starting_type = models.ForeignKey('GroupTypes', on_delete=models.CASCADE, related_name='player_groups')
    current_type = models.ForeignKey('GroupTypes', on_delete=models.CASCADE, related_name='current_player_groups')
    
class GroupTypes(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    
    def __str__(self):
        return str(self.name)

class PlayerChange(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    player_in = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='player_changes')
    player_out = models.ForeignKey(Player, on_delete=models.CASCADE)
    player_group = models.ForeignKey(PlayerGroup, on_delete=models.CASCADE, related_name='player_changes')
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='player_changes', blank=True, null=True)
    match_part = models.ForeignKey(MatchPart, on_delete=models.CASCADE, related_name='player_changes', blank=True, null=True)
    time = models.DateTimeField(default=None, blank=True, null=True)

class Goal(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='goals')
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='goals')
    match_part = models.ForeignKey(MatchPart, on_delete=models.CASCADE, related_name='goals', blank=True, null=True)
    time = models.DateTimeField(default=None, blank=True, null=True)
    goal_type = models.ForeignKey('GoalType', on_delete=models.CASCADE, related_name='goals')
    for_team = models.BooleanField(default=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='goals', blank=True, null=True)

class GoalType(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    
    def __str__(self):
        return str(self.name)
    
class Shot(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='shots')
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='shots')
    match_part = models.ForeignKey(MatchPart, on_delete=models.CASCADE, related_name='shots', blank=True, null=True)
    time = models.DateTimeField(default=None, blank=True, null=True)
    for_team = models.BooleanField(default=True)

class Pause(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='pauses')
    match_part = models.ForeignKey(MatchPart, on_delete=models.CASCADE, related_name='pauses', blank=True, null=True)
    time = models.DateTimeField(default=None, blank=True, null=True)
    end_time = models.DateTimeField(blank=True, null=True)
    length = models.IntegerField(blank=True, null=True)
    active = models.BooleanField(default=True)

class Season(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    
    def __str__(self):
        return str(self.name)
    
class PageConnectRegistration(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='page_connect_registrations')
    page = models.CharField(max_length=255)
    registration_date = models.DateTimeField(auto_now_add=True)
    counter = models.IntegerField(default=0)
