"""Track native roster changes made through M2M services or the admin."""

from uuid import UUID

from django.db import models
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from apps.player.models import Player
from apps.team.models import TeamData
from apps.team.services.roster_history import reconcile_roster_history


@receiver(m2m_changed, sender=TeamData.players.through)
@receiver(m2m_changed, sender=TeamData.coach.through)
@receiver(m2m_changed, sender=TeamData.staff.through)
def record_roster_change(
    sender: type[models.Model],
    instance: TeamData | Player,
    action: str,
    reverse: bool,
    pk_set: set[int | UUID] | None,
    **kwargs: object,
) -> None:
    """Lock before M2M writes and reconcile both forward and reverse operations."""
    role = next(
        role
        for role in ("players", "coach", "staff")
        if getattr(TeamData, role).through is sender
    )
    if action.startswith("pre_"):
        ids = (
            (
                pk_set
                if pk_set is not None
                else sender._default_manager.filter(player_id=instance.pk).values_list(
                    "teamdata_id", flat=True
                )
            )
            if reverse
            else [instance.pk]
        )
        rows = list(
            TeamData.objects.select_for_update().filter(pk__in=ids).order_by("pk")
        )
        setattr(instance, f"_roster_history_{role}", rows)
    elif action.startswith("post_"):
        for row in getattr(instance, f"_roster_history_{role}", []):
            reconcile_roster_history(team_data=row, role=role)
