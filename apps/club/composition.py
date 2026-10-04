"""Bind the club application's join-request ports to production capabilities."""

from __future__ import annotations

from apps.club.services.join_requests import JoinRequestEvent, JoinRequestPorts
from apps.kwt_common.services.jobs import enqueue
from apps.player.models import Player
from apps.schedule.queries.seasons import current_season, most_recent_season
from apps.team.models import Team
from apps.team.services.roster import change_team_membership


def _add_to_team(*, team: Team, player: Player) -> None:
    season = current_season() or most_recent_season()
    if season is not None:
        change_team_membership(team=team, season=season, player=player, operation="add")


def _job(task: str, *, once: bool) -> JoinRequestEvent:
    def schedule(*, request_id: str) -> None:
        enqueue(
            f"apps.club.tasks.{task}",
            request_id,
            kwargs={"request_id": request_id},
            once=once,
        )

    return schedule


join_request_ports = JoinRequestPorts(
    add_to_team=_add_to_team,
    # Each request is announced once and decided once.
    notify_reviewers=_job("notify_join_request_reviewers", once=True),
    notify_requester=_job("notify_join_request_decision", once=True),
    schedule_identity_link=_job("link_join_request_identity", once=False),
)
