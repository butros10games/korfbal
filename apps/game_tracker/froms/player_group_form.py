"""Forms for the PlayerGroup model."""

from django import forms

from ..models import PlayerGroup


class PlayerGroupForm(forms.ModelForm):
    """Form for the PlayerGroup model."""

    class Meta:
        """Meta class for the PlayerGroupForm."""

        model = PlayerGroup
        fields = ["players", "team", "match_data", "starting_type", "current_type"]

    def __init__(self):
        """Initialize the PlayerGroupForm."""
        super().__init__()
        if self.instance and self.instance.match_data:
            # Limit the queryset for players to those in the match data
            self.fields["players"].queryset = self.instance.match_data.players.all()
