"""Application services for match and MVP notifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Protocol

from django.utils import timezone

from apps.awards.models.mvp import MatchMvpVote
from apps.awards.services import mvp as mvp_service
from apps.game_tracker.models import MatchData
from apps.player.models.player import Player
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.expo_push import ExpoPushPayload
from apps.player.services.web_push import WebPushPayload
from apps.schedule.models.match import Match


logger = logging.getLogger(__name__)


class IdempotencyClaim(Protocol):
    """Claim a short-lived idempotency key."""

    def __call__(self, key: str, timeout_seconds: int, /) -> bool:
        """Return whether the key was claimed by this caller."""
        ...


class PayloadSender(Protocol):
    """Deliver one notification payload to user accounts."""

    def __call__(
        self,
        *,
        user_ids: list[int],
        payload: WebPushPayload,
    ) -> None:
        """Deliver the payload."""
        ...


class TaskScheduler(Protocol):
    """Schedule a follow-up MVP task."""

    def __call__(self, *, match_id: str, eta: datetime) -> None:
        """Schedule the task."""
        ...


class WebPushSender(Protocol):
    """Deliver one web-push notification."""

    def __call__(
        self,
        *,
        sub: PlayerPushSubscription,
        payload: WebPushPayload,
    ) -> None:
        """Deliver the web-push notification."""
        ...


class ExpoPushSender(Protocol):
    """Deliver one Expo notification to a token batch."""

    def __call__(
        self,
        *,
        tokens: list[str],
        payload: ExpoPushPayload,
    ) -> None:
        """Deliver the Expo notification."""
        ...


@dataclass(frozen=True)
class FinishedMatchJobs:
    """Runtime capabilities used when a match reaches its terminal state."""

    claim_once: IdempotencyClaim
    send_payload: PayloadSender
    schedule_reminder: TaskScheduler
    schedule_publish: TaskScheduler


def _participant_players(match_data: MatchData) -> list[Player]:
    return list(
        Player.objects
        .select_related("user")
        .filter(match_players__match_data=match_data)
        .distinct()
    )


def _participant_user_ids(players: list[Player]) -> list[int]:
    user_ids: list[int] = []
    for player in players:
        user_id = getattr(getattr(player, "user", None), "pk", None)
        if isinstance(user_id, int):
            user_ids.append(user_id)
    return user_ids


def _match_title(match: Match) -> str:
    home = getattr(match.home_team, "name", "") or "Thuis"
    away = getattr(match.away_team, "name", "") or "Uit"
    return f"{home} - {away}".strip(" -")


def _push_url(match: Match) -> str:
    return f"/matches/{match.id_uuid}"


def send_payload_to_users(
    *,
    user_ids: list[int],
    payload: WebPushPayload,
    send_web_push: WebPushSender,
    send_expo_push: ExpoPushSender,
) -> None:
    """Fan a notification out to active web and Expo subscriptions."""
    if not user_ids:
        return

    subscriptions = PlayerPushSubscription.objects.filter(
        user_id__in=user_ids,
        is_active=True,
    )
    expo_tokens: list[str] = []

    for subscription in subscriptions:
        if subscription.platform == "expo":
            expo_tokens.append(subscription.endpoint)
        else:
            send_web_push(sub=subscription, payload=payload)

    if expo_tokens:
        send_expo_push(
            tokens=expo_tokens,
            payload=ExpoPushPayload(
                title=payload.title,
                body=payload.body,
                url=payload.url,
            ),
        )


def handle_finished_match(
    *,
    match_id: str,
    match_data_id: str,
    jobs: FinishedMatchJobs,
) -> None:
    """Notify match participants and schedule the MVP lifecycle."""
    match = (
        Match.objects
        .select_related(
            "home_team",
            "away_team",
            "home_team__club",
            "away_team__club",
        )
        .filter(id_uuid=match_id)
        .first()
    )
    match_data = (
        MatchData.objects
        .select_related(
            "match_link",
            "match_link__home_team",
            "match_link__away_team",
        )
        .filter(id_uuid=match_data_id)
        .first()
    )
    if match is None or match_data is None or match_data.status != "finished":
        return

    # Claim only after the committed terminal state is visible. A worker can run
    # before the transaction that finished the match commits and then be retried.
    if not jobs.claim_once(f"push:match_finished:{match_data_id}", 60 * 60 * 24):
        return

    home = getattr(match.home_team, "name", "") or "Thuis"
    away = getattr(match.away_team, "name", "") or "Uit"
    jobs.send_payload(
        user_ids=_participant_user_ids(_participant_players(match_data)),
        payload=WebPushPayload(
            title="Wedstrijd afgelopen",
            body=(f"{home} {match_data.home_score} - {match_data.away_score} {away}"),
            url=_push_url(match),
            tag=f"match-finished:{match_data.id_uuid}",
        ),
    )

    try:
        match_mvp = mvp_service.get_or_create_match_mvp(match, match_data)
    except Exception:
        logger.warning("Failed to ensure MatchMvp for %s", match_id, exc_info=True)
        return

    reminder_at = match_mvp.closes_at - timedelta(hours=1)
    if reminder_at > timezone.now():
        jobs.schedule_reminder(match_id=match_id, eta=reminder_at)

    publish_at = match_mvp.closes_at + timedelta(minutes=1)
    if publish_at > timezone.now():
        jobs.schedule_publish(match_id=match_id, eta=publish_at)


def remind_mvp_voters(
    *,
    match_id: str,
    send_payload: PayloadSender,
) -> None:
    """Remind match participants who have not cast an MVP vote."""
    match = (
        Match.objects
        .select_related("home_team", "away_team")
        .filter(id_uuid=match_id)
        .first()
    )
    match_data = MatchData.objects.filter(match_link_id=match_id).first()
    if match is None or match_data is None:
        return

    try:
        match_mvp = mvp_service.get_or_create_match_mvp(match, match_data)
    except Exception:
        logger.warning("Failed to load MatchMvp for %s", match_id, exc_info=True)
        return

    now = timezone.now()
    if now < match_mvp.closes_at - timedelta(hours=1) or now >= match_mvp.closes_at:
        return

    participants = _participant_players(match_data)
    if not participants:
        return

    participant_ids = [player.id_uuid for player in participants]
    voted_ids = set(
        MatchMvpVote.objects.filter(
            match_id=match_id, voter_id__in=participant_ids
        ).values_list("voter_id", flat=True)
    )
    missing_vote_user_ids = [
        int(player.user.pk)
        for player in participants
        if isinstance(getattr(player.user, "pk", None), int)
        and player.id_uuid not in voted_ids
    ]

    send_payload(
        user_ids=missing_vote_user_ids,
        payload=WebPushPayload(
            title="MVP stemmen",
            body=f"Nog 1 uur om te stemmen voor {_match_title(match)}",
            url=_push_url(match),
            tag=f"mvp-reminder:{match_id}",
        ),
    )


def publish_mvp(
    *,
    match_id: str,
    send_payload: PayloadSender,
) -> None:
    """Publish a closed MVP vote and notify match participants once."""
    match = (
        Match.objects
        .select_related("home_team", "away_team")
        .filter(id_uuid=match_id)
        .first()
    )
    match_data = MatchData.objects.filter(match_link_id=match_id).first()
    if match is None or match_data is None:
        return

    before = mvp_service.get_or_create_match_mvp(match, match_data)
    was_published = bool(before.published_at)
    after = mvp_service.ensure_mvp_published(match, match_data)
    if not after.published_at or was_published:
        return

    winner_name = None
    if after.mvp_player is not None:
        winner_name = (
            after.mvp_player.user.get_full_name() or after.mvp_player.user.username
        )
    title = _match_title(match)
    body = (
        f"MVP voor {title}: {winner_name}"
        if winner_name
        else f"MVP voor {title} is bekend."
    )

    send_payload(
        user_ids=_participant_user_ids(_participant_players(match_data)),
        payload=WebPushPayload(
            title="MVP bekend",
            body=body,
            url=_push_url(match),
            tag=f"mvp-published:{match_id}",
        ),
    )
