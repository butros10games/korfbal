from django.db import models
from django.urls import reverse

from uuidv7 import uuid7


class Team(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255)
    club = models.ForeignKey('club.Club', on_delete=models.CASCADE, related_name='teams')
    
    def __str__(self):
        return str(self.club.name) + " " + str(self.name)
    
    def get_absolute_url(self):
        return reverse('team_detail', kwargs={'team_id': self.id_uuid})
