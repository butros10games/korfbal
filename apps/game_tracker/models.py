from django.db import models
from django.core.exceptions import ValidationError

from uuidv7 import uuid7

team_model_string = 'team.Team'
player_model_string = 'player.Player'

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
    
    players = models.ManyToManyField(player_model_string, related_name='match_data', blank=True)

    def get_winner(self):
        if self.home_score > self.away_score:
            return self.home_team
        elif self.home_score < self.away_score:
            return self.away_team
        else:
            return None
        
    def __str__(self):
        return str(self.match_link.home_team.name + " - " + self.match_link.away_team.name)
    
class MatchPart(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match_data = models.ForeignKey('MatchData', on_delete=models.CASCADE, related_name='match_parts')
    part_number = models.IntegerField()
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True, null=True)

class PlayerGroup(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    players = models.ManyToManyField(player_model_string, related_name='player_groups', blank=True)
    team = models.ForeignKey(team_model_string, on_delete=models.CASCADE, related_name='player_groups')
    match_data = models.ForeignKey('MatchData', on_delete=models.CASCADE, related_name='player_groups')
    starting_type = models.ForeignKey('GroupTypes', on_delete=models.CASCADE, related_name='player_groups')
    current_type = models.ForeignKey('GroupTypes', on_delete=models.CASCADE, related_name='current_player_groups')

    def clean(self):
        # Ensure that all selected players are part of the match's players field
        valid_players = self.match_data.players.all()
        invalid_players = self.players.exclude(id__in=valid_players)

        if invalid_players.exists():
            raise ValidationError(f"Invalid players selected: {', '.join([str(player) for player in invalid_players])}. Players must be part of the match data.")

    def __str__(self):
        return f"Player Group {self.id_uuid}"

    
class GroupTypes(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    
    def __str__(self):
        return str(self.name)

class PlayerChange(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    player_in = models.ForeignKey(player_model_string, on_delete=models.CASCADE, related_name='player_changes')
    player_out = models.ForeignKey(player_model_string, on_delete=models.CASCADE)
    player_group = models.ForeignKey('PlayerGroup', on_delete=models.CASCADE, related_name='player_changes')
    match_data = models.ForeignKey('MatchData', on_delete=models.CASCADE, related_name='player_changes', blank=True, null=True)
    match_part = models.ForeignKey('MatchPart', on_delete=models.CASCADE, related_name='player_changes', blank=True, null=True)
    time = models.DateTimeField(default=None, blank=True, null=True)

class GoalType(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    
    def __str__(self):
        return str(self.name)
    
class Shot(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    player = models.ForeignKey(player_model_string, on_delete=models.CASCADE, related_name='shots')
    match_data = models.ForeignKey('MatchData', on_delete=models.CASCADE, related_name='shots')
    match_part = models.ForeignKey('MatchPart', on_delete=models.CASCADE, related_name='shots', blank=True, null=True)
    team = models.ForeignKey(team_model_string, on_delete=models.CASCADE, related_name='shots', blank=True, null=True)
    for_team = models.BooleanField(default=True)
    scored = models.BooleanField(default=False)
    shot_type = models.ForeignKey('GoalType', on_delete=models.CASCADE, related_name='shots', blank=True, null=True)
    time = models.DateTimeField(default=None, blank=True, null=True)

class Pause(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match_data = models.ForeignKey('MatchData', on_delete=models.CASCADE, related_name='pauses')
    match_part = models.ForeignKey('MatchPart', on_delete=models.CASCADE, related_name='pauses', blank=True, null=True)
    start_time = models.DateTimeField(default=None, blank=True, null=True)
    end_time = models.DateTimeField(blank=True, null=True)
    
    def length(self):
        return self.end_time - self.start_time
