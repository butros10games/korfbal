from django.db import models

from uuidv7 import uuid7

player_model_string = 'player.Player'


class PageConnectRegistration(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    player = models.ForeignKey(player_model_string, on_delete=models.CASCADE, related_name='page_connect_registrations')
    page = models.CharField(max_length=255)
    registration_date = models.DateTimeField(auto_now_add=True)
