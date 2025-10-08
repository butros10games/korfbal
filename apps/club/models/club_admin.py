"""Model for ClubAdmin."""

from typing import Any

from django.db import models


class ClubAdmin(models.Model):
    """Model for a club admin."""

    club: models.ForeignKey[Any, Any] = models.ForeignKey(
        "Club", on_delete=models.CASCADE
    )
    player: models.ForeignKey[Any, Any] = models.ForeignKey(
        "player.Player", on_delete=models.CASCADE
    )

    class Meta:
        """Meta class for the ClubAdmin model."""

        unique_together = ("club", "player")

    def __str__(self) -> str:
        """Return the string representation of the ClubAdmin.

        Returns:
            str: A string representation of the ClubAdmin.

        """
        return f"{self.player} - {self.club}"
