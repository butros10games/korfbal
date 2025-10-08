"""Forms for the PlayerGroup model."""

from typing import ClassVar

from django import forms

from apps.game_tracker.models import PlayerGroup


class PlayerGroupForm(forms.ModelForm[PlayerGroup]):
    """Form for the PlayerGroup model."""

    class Meta:
        """Meta class for the PlayerGroupForm."""

        model = PlayerGroup
        fields: ClassVar[list[str]] = [
            "players",
            "team",
            "match_data",
            "starting_type",
            "current_type",
        ]

    def __init__(self) -> None:
        """Initialize the PlayerGroupForm."""
        super().__init__()
        if self.instance and self.instance.match_data:
            # Limit the queryset for players to those in the match data
            self.fields["players"].queryset = self.instance.match_data.players.all()  # type: ignore
