"""Historical native roster roles, independent of match lineup assignments."""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class TeamRosterMembership(models.Model):
    """One observed membership interval; unknown legacy start times stay unknown."""

    team_data = models.ForeignKey(
        "team.TeamData", on_delete=models.CASCADE, related_name="membership_history"
    )
    player = models.ForeignKey(
        "player.Player", on_delete=models.PROTECT, related_name="roster_history"
    )
    team_data_id: int

    role = models.CharField(
        max_length=16,
        choices=[("players", "Player"), ("coach", "Coach"), ("staff", "Staff")],
    )
    started_at = models.DateTimeField(default=timezone.now, null=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=32, default="native")

    class Meta:
        """Keep one open interval per role without forbidding multiple roles."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["team_data", "player", "role"],
                condition=models.Q(ended_at__isnull=True),
                name="unique_active_native_roster_role",
            ),
            models.CheckConstraint(
                condition=models.Q(started_at__isnull=True)
                | models.Q(ended_at__isnull=True)
                | models.Q(ended_at__gte=models.F("started_at")),
                name="native_roster_valid_interval",
            ),
        ]

    def __str__(self) -> str:
        """Identify the historical role without fetching related people."""
        return f"Roster membership {self.pk}: {self.role}"
