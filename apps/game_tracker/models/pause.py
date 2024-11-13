from django.db import models
from datetime import timedelta

from uuidv7 import uuid7


class Pause(models.Model):
    id_uuid = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    match_data = models.ForeignKey("MatchData", on_delete=models.CASCADE, related_name="pauses")
    match_part = models.ForeignKey("MatchPart", on_delete=models.CASCADE, related_name="pauses", blank=True, null=True)
    start_time = models.DateTimeField(default=None, blank=True, null=True)
    end_time = models.DateTimeField(blank=True, null=True)
    active = models.BooleanField(default=False)

    def length(self):
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return timedelta(0)
