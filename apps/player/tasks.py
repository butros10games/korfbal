"""Stable Celery adapters for the player application services."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from celery import shared_task

from apps.competition.services.schedule_notifications import notify_schedule_change
from apps.kwt_common.services.jobs import enqueue
from apps.player.composition import (
    download_spotify_track,
    expo_push_client,
    prepare_player_song_clip,
    send_web_push,
)
from apps.player.models.player_song import PlayerSong
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.expo_push import ExpoPushPayload
from apps.player.services.match_notifications import (
    FinishedMatchJobs,
    handle_finished_match,
    publish_mvp,
    remind_mvp_voters,
)
from apps.player.services.song_processing import (
    process_cached_song_download,
    process_player_song_download,
)
from apps.player.services.web_push import WebPushPayload


def _send_payload(*, user_ids: list[int], payload: WebPushPayload) -> None:
    for subscription_id, user_id in PlayerPushSubscription.objects.filter(
        user_id__in=user_ids, is_active=True
    ).values_list("pk", "user_id"):
        enqueue(
            "apps.player.tasks.deliver_notification",
            f"{payload.tag}:{subscription_id}",
            kwargs={
                "subscription_id": str(subscription_id),
                "user_id": user_id,
                "payload": asdict(payload),
            },
            once=True,
        )


def _schedule_reminder(*, match_id: str, eta: datetime) -> None:
    enqueue(
        "apps.player.tasks.send_mvp_vote_reminder",
        match_id,
        kwargs={"match_id": match_id},
        due_at=eta,
        once=True,
    )


def _schedule_publish(*, match_id: str, eta: datetime) -> None:
    enqueue(
        "apps.player.tasks.publish_mvp_and_notify",
        match_id,
        kwargs={"match_id": match_id},
        due_at=eta,
        once=True,
    )


def _dispatch_cached_song(cached_song_id: str) -> None:
    enqueue(
        "apps.player.tasks.download_cached_song",
        cached_song_id,
        args=[cached_song_id],
        queue="media",
    )


@shared_task(bind=True)
def handle_match_finished(
    self: Any,
    *,
    match_id: str,
    match_data_id: str,
) -> None:
    """Notify participants and schedule MVP jobs after a finished match."""
    handle_finished_match(
        match_id=match_id,
        match_data_id=match_data_id,
        jobs=FinishedMatchJobs(
            send_payload=_send_payload,
            schedule_reminder=_schedule_reminder,
            schedule_publish=_schedule_publish,
        ),
    )


@shared_task(bind=True)
def send_mvp_vote_reminder(self: Any, *, match_id: str) -> None:
    """Notify participants who have not cast their MVP vote."""
    remind_mvp_voters(match_id=match_id, send_payload=_send_payload)


@shared_task(bind=True)
def publish_mvp_and_notify(self: Any, *, match_id: str) -> None:
    """Publish a closed MVP vote and notify its participants."""
    publish_mvp(match_id=match_id, send_payload=_send_payload)


def _queue_clip(song: PlayerSong) -> None:
    enqueue(
        "apps.player.tasks.download_player_song",
        str(song.pk),
        args=[str(song.pk)],
        queue="media",
    )


def _prepare_clip(song: PlayerSong) -> str:
    clip = prepare_player_song_clip(song)
    if clip is None:
        raise RuntimeError("Goal-song clip could not be prepared")
    return clip


@shared_task(bind=True)
def download_cached_song(self: Any, cached_song_id: str) -> None:
    """Download a shared song and prepare its dependent player clips."""
    process_cached_song_download(
        cached_song_id,
        download_track=download_spotify_track,
        prepare_clip=_queue_clip,
    )


@shared_task(bind=True)
def download_player_song(self: Any, song_id: str) -> None:
    """Process a legacy or shared-cache-backed player song."""
    process_player_song_download(
        song_id,
        dispatch_cached_song=_dispatch_cached_song,
        download_track=download_spotify_track,
        prepare_clip=_prepare_clip,
    )


@shared_task
def notify_official_schedule_change(
    *, notification_id: str, match_id: str, starts_at: str, cancelled: bool
) -> None:
    """Deliver a committed KNKV schedule change through existing push transports."""
    notify_schedule_change(
        notification_id=notification_id,
        match_id=match_id,
        starts_at=starts_at,
        cancelled=cancelled,
        send_payload=_send_payload,
    )


@shared_task(ignore_result=True)
def deliver_notification(*, subscription_id: str, user_id: int, payload: dict) -> None:
    """Retry individual destinations without resending successful recipients."""
    subscription = PlayerPushSubscription.objects.filter(
        pk=subscription_id, user_id=user_id, is_active=True
    ).first()
    if subscription is None:
        return
    notification = WebPushPayload(**payload)
    if subscription.platform == "expo":
        expo_push_client.send_messages([
            ExpoPushPayload(
                title=notification.title, body=notification.body, url=notification.url
            ).to_message(subscription.endpoint)
        ])
    else:
        send_web_push(sub=subscription, payload=notification)
