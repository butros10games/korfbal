from django.db import models
from django.urls import reverse

from uuidv7 import uuid7

class Club(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)
    admin = models.ManyToManyField('game_tracker.Player', through='ClubAdmin', related_name='clubs', blank=True)
    logo = models.ImageField(upload_to='media/club_pictures/', default='/static/images/clubs/blank-club-picture.png', blank=True)
    
    def __str__(self):
        return str(self.name)
    
    def get_absolute_url(self):
        return reverse("club_detail", kwargs={"club_id": self.id_uuid})

class ClubAdmin(models.Model):
    club = models.ForeignKey('Club', on_delete=models.CASCADE)
    player = models.ForeignKey('game_tracker.Player', on_delete=models.CASCADE)

    class Meta:
        unique_together = ('club', 'player')  # Optional: Ensure a player-club pair is unique

    def __str__(self):
        return f"{self.player} - {self.club}"