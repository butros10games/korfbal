"""Detect official fixture changes without announcing initial catalogue imports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.competition.models import Match as SourceMatch
from apps.player.models import Player
from apps.player.services.match_notifications import PayloadSender
from apps.player.services.web_push import WebPushPayload
from apps.schedule.models import Match


class ScheduleChangeDispatcher(Protocol):
    """Queue a notification only after its catalogue transaction commits."""

    def __call__(
        self, *, notification_id: str, match_id: str, starts_at: str, cancelled: bool
    ) -> None:
        """Dispatch an official schedule change."""


def schedule_changed(previous: dict, current: dict) -> bool:
    """Ignore initial discovery, historical corrections and routine result updates."""
    if not previous or current["status"] not in {"SCHEDULED", "CANCELLED"}:
        return False
    if previous.get("status") not in {"SCHEDULED", "CANCELLED"}:
        return False
    old_time = datetime.fromisoformat(previous["starts_at"])
    new_time = datetime.fromisoformat(current["starts_at"])
    if max(old_time, new_time) <= timezone.now():
        return False
    return old_time != new_time or previous["status"] != current["status"]


@transaction.atomic
def notify_schedule_change(
    *,
    notification_id: str,
    match_id: str,
    starts_at: str,
    cancelled: bool,
    send_payload: PayloadSender,
) -> None:
    """Notify current followers once per user without importing personal identities."""
    expected = {
        "starts_at": starts_at,
        "status": "CANCELLED" if cancelled else "SCHEDULED",
    }
    # Claim the exact publication once, even if a later schedule repeats its values.
    # The claim and per-recipient durable delivery intents commit together.
    if not SourceMatch.objects.filter(
        local_match_id=match_id,
        published_schedule=expected,
        schedule_notification_id=notification_id,
    ).update(schedule_notification_id=None):
        return
    match = (
        Match.objects
        .select_related("home_team__club", "away_team__club")
        .filter(pk=match_id)
        .first()
    )
    if match is None:
        return
    recipients = list(
        Player.objects.filter(
            Q(
                pk__in=Player.team_follow.through.objects.filter(
                    team_id__in=[match.home_team_id, match.away_team_id]
                ).values("player_id")
            )
            | Q(
                pk__in=Player.club_follow.through.objects.filter(
                    club_id__in=[match.home_team.club_id, match.away_team.club_id]
                ).values("player_id")
            ),
            user__isnull=False,
            user__is_active=True,
        ).values_list("user_id", flat=True)
    )
    if not recipients:
        return
    title = "Wedstrijd afgelast" if cancelled else "Wedstrijdprogramma gewijzigd"
    name = f"{match.home_team} - {match.away_team}"
    start = datetime.fromisoformat(starts_at).astimezone(ZoneInfo("Europe/Amsterdam"))
    body = (
        f"{name} is afgelast volgens KNKV."
        if cancelled
        else f"{name}: {start:%d-%m-%Y om %H:%M}. Bekijk het actuele programma."
    )
    send_payload(
        user_ids=recipients,
        payload=WebPushPayload(
            title=title,
            body=body,
            url=match.get_absolute_url(),
            tag=f"schedule:{notification_id}",
        ),
    )
