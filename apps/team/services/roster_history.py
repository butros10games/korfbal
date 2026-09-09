"""Keep roster history aligned with current native membership projections."""

from django.utils import timezone

from apps.team.models import TeamData, TeamRosterMembership


def reconcile_roster_history(
    *, team_data: TeamData, role: str, source: str = "native"
) -> None:
    """Record changes under the caller's TeamData lock and transaction."""
    through = getattr(TeamData, role).through
    present = set(
        through.objects.filter(teamdata_id=team_data.pk).values_list(
            "player_id", flat=True
        )
    )
    active = TeamRosterMembership.objects.filter(
        team_data=team_data, role=role, ended_at__isnull=True
    )
    known = set(active.values_list("player_id", flat=True))
    now = timezone.now()
    active.exclude(player_id__in=present).update(ended_at=now)
    TeamRosterMembership.objects.bulk_create([
        TeamRosterMembership(
            team_data=team_data,
            player_id=player_id,
            role=role,
            started_at=now,
            source=source,
        )
        for player_id in present - known
    ])
