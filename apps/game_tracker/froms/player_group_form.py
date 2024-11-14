from django import forms

from ..models import PlayerGroup


class PlayerGroupForm(forms.ModelForm):
    class Meta:
        model = PlayerGroup
        fields = ["players", "team", "match_data", "starting_type", "current_type"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.match_data:
            # Limit the queryset for players to those in the match data
            self.fields["players"].queryset = self.instance.match_data.players.all()
