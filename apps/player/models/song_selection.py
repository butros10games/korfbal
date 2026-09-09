"""Ordered, foreign-key-backed goal song selections."""

from typing import ClassVar

from django.db import models


class PlayerGoalSongSelection(models.Model):
    """One position in a player's preferred goal songs."""

    player = models.ForeignKey(
        "player.Player", on_delete=models.CASCADE, related_name="goal_song_selections"
    )
    song = models.ForeignKey("player.PlayerSong", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()

    class Meta:
        """Keep selection order and identity unambiguous."""

        ordering = ("position",)
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["player", "position"], name="unique_player_song_position"
            ),
            models.UniqueConstraint(
                fields=["player", "song"], name="unique_player_song_selection"
            ),
        ]

    def __str__(self) -> str:
        """Return a stable selection identifier without fetching relations."""
        return f"Player song selection {self.pk}"


class TeamGoalSongSelection(models.Model):
    """One position in a seasonal team's fallback songs."""

    team_data = models.ForeignKey(
        "team.TeamData", on_delete=models.CASCADE, related_name="goal_song_selections"
    )
    song = models.ForeignKey("player.PlayerSong", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()

    class Meta:
        """Keep selection order and identity unambiguous."""

        ordering = ("position",)
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["team_data", "position"], name="unique_team_song_position"
            ),
            models.UniqueConstraint(
                fields=["team_data", "song"], name="unique_team_song_selection"
            ),
        ]

    def __str__(self) -> str:
        """Return a stable selection identifier without fetching relations."""
        return f"Team song selection {self.pk}"
