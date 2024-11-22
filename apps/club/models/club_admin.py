from django.db import models


class ClubAdmin(models.Model):
    club = models.ForeignKey("Club", on_delete=models.CASCADE)
    player = models.ForeignKey("player.Player", on_delete=models.CASCADE)

    class Meta:
        unique_together = ("club", "player")

    def __str__(self) -> str:
        return f"{self.player} - {self.club}"
