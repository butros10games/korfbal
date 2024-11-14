from django.db import models

from uuidv7 import uuid7


class MatchPart(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match_data = models.ForeignKey(
        "MatchData", on_delete=models.CASCADE, related_name="match_parts"
    )
    part_number = models.IntegerField()
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True, null=True)
    active = models.BooleanField(default=False)
