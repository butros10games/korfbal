from django.db import models

from uuidv7 import uuid7


class GoalType(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return str(self.name)
