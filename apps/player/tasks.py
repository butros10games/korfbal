"""Stable Celery adapters for the player application services."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from celery import shared_task
from django.core.cache import cache

from apps.player.composition import (
    download_spotify_track,
    prepare_player_song_clip,
    send_expo_push,
    send_web_push,
)
from apps.player.services.match_notifications import (
    FinishedMatchJobs,
    handle_finished_match,
    publish_mvp,
    remind_mvp_voters,
    send_payload_to_users,
)
from apps.player.services.song_processing import (
    process_cached_song_download,
    process_player_song_download,
)
from apps.player.services.web_push import WebPushPayload


def _claim_once(key: str, timeout_seconds: int) -> bool:
    return cache.add(key, "1", timeout=timeout_seconds)


def _send_payload(*, user_ids: list[int], payload: WebPushPayload) -> None:
    send_payload_to_users(
        user_ids=user_ids,
        payload=payload,
        send_web_push=send_web_push,
        send_expo_push=send_expo_push,
    )


def _schedule_reminder(*, match_id: str, eta: datetime) -> None:
    send_mvp_vote_reminder.apply_async(kwargs={"match_id": match_id}, eta=eta)


def _schedule_publish(*, match_id: str, eta: datetime) -> None:
    publish_mvp_and_notify.apply_async(kwargs={"match_id": match_id}, eta=eta)


def _dispatch_cached_song(cached_song_id: str) -> None:
    download_cached_song.apply(args=[cached_song_id])


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
            claim_once=_claim_once,
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


@shared_task(bind=True)
def download_cached_song(self: Any, cached_song_id: str) -> None:
    """Download a shared song and prepare its dependent player clips."""
    process_cached_song_download(
        cached_song_id,
        download_track=download_spotify_track,
        prepare_clip=prepare_player_song_clip,
    )


@shared_task(bind=True)
def download_player_song(self: Any, song_id: str) -> None:
    """Process a legacy or shared-cache-backed player song."""
    process_player_song_download(
        song_id,
        dispatch_cached_song=_dispatch_cached_song,
        download_track=download_spotify_track,
        prepare_clip=prepare_player_song_clip,
    )
