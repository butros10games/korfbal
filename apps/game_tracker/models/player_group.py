"""Module contains the PlayerGroup model."""

from __future__ import annotations

from typing import Any

from bg_uuidv7 import uuidv7
from django.core.exceptions import ValidationError
from django.db import models

from .constants import player_model_string, team_model_string


class PlayerGroup(models.Model):
    """Model for a group of players in a match."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    players: models.ManyToManyField[Any, Any] = models.ManyToManyField(
        player_model_string,
        related_name="player_groups",
        blank=True,
    )
    team: models.ForeignKey[Any, Any] = models.ForeignKey(
        team_model_string,
        on_delete=models.CASCADE,
        related_name="player_groups",
    )
    match_data: models.ForeignKey[Any, Any] = models.ForeignKey(
        "MatchData",
        on_delete=models.CASCADE,
        related_name="player_groups",
    )
    starting_type: models.ForeignKey[Any, Any] = models.ForeignKey(
        "GroupType",
        on_delete=models.CASCADE,
        related_name="player_groups",
    )
    current_type: models.ForeignKey[Any, Any] = models.ForeignKey(
        "GroupType",
        on_delete=models.CASCADE,
        related_name="current_player_groups",
    )

    def __str__(self) -> str:
        """Return the string representation of the player group.

        Returns:
            str: A string representation of the player group.

        """
        return f"Player Group {self.id_uuid} - {self.team} - {self.match_data} - {self.starting_type} - {self.current_type}"  # noqa: E501

    def save(self, *args: tuple[object, ...], **kwargs: dict[str, object]) -> None:  # type: ignore[override]
        """Save the player group, ensuring that players are removed from the reserve
            group if they are added to a starting group.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Raises:
            ValidationError: If a player is not in the reserve group when trying to add
                them to a starting group.

        """
        if not self._state.adding:
            # Retrieve the current list of players from the database
            current_players = set(self.__class__.objects.get(pk=self.pk).players.all())  # type: ignore[attr-defined]
        else:
            current_players = set()

        new_players = set(self.players.all()) - current_players

        if self.starting_type.name != "Reserve" and new_players:
            reserve_player_group = self.match_data.player_groups.get(
                starting_type__name="Reserve",
                team=self.team,
            )
            for player in new_players:
                if player in reserve_player_group.players.all():
                    reserve_player_group.players.remove(player)
                else:
                    raise ValidationError(
                        f"{player} is not in the reserve player group.",
                    )

        super().save(*args, **kwargs)  # type: ignore[arg-type]
