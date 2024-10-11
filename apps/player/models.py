from django.db import models
from django.urls import reverse
from uuidv7 import uuid7

from django.contrib.auth.models import User

team_model_string = 'team.Team'
club_model_string = 'club.Club'

class Player(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='players')
    
    profile_picture = models.ImageField(upload_to='media/profile_pictures/', default='/static/images/player/blank-profile-picture.png', blank=True)
    
    team_follow = models.ManyToManyField(team_model_string, blank=True)
    club_follow = models.ManyToManyField(club_model_string, blank=True)
    
    def __str__(self):
        return str(self.user.username)
    
    def get_absolute_url(self):
        return reverse('profile_detail', kwargs={'player_id': self.id_uuid})