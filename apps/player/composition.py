"""Composition helpers that wire player use cases to outbound adapters."""

from __future__ import annotations

from functools import partial

from django.conf import settings

from apps.player.adapters.outbound.command_runner import SubprocessCommandRunner
from apps.player.adapters.outbound.expo_push import RequestsExpoPushClient
from apps.player.adapters.outbound.song_jobs import CelerySongDownloadDispatcher
from apps.player.adapters.outbound.spotify import RequestsSpotifyClient
from apps.player.adapters.outbound.storage import DjangoAudioStorage
from apps.player.adapters.outbound.web_push import PyWebPushClient
from apps.player.application.ports import AudioRuntime
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.expo_push import send_expo_push_tokens
from apps.player.services.player_audio import (
    ensure_goal_song_clip as _ensure_goal_song_clip,
    prepare_player_song_clip as _prepare_player_song_clip,
)
from apps.player.services.player_songs import (
    create_player_song as _create_player_song,
    enqueue_download_for_player_song as _enqueue_download,
    retry_owned_player_song_download as _retry_owned_song,
    update_player_song_settings as _update_song_settings,
)
from apps.player.services.push_notifications import (
    missing_webpush_settings,
    send_test_payload as _send_test_payload,
)
from apps.player.services.spotdl import (
    download_spotify_track as _download_spotify_track,
)
from apps.player.services.web_push import (
    WebPushPayload,
    send_to_model_subscription,
)


command_runner = SubprocessCommandRunner()
audio_storage = DjangoAudioStorage()
audio_runtime = AudioRuntime(storage=audio_storage, commands=command_runner)
song_jobs = CelerySongDownloadDispatcher()
web_push_client = PyWebPushClient()
expo_push_client = RequestsExpoPushClient()
spotify_client = RequestsSpotifyClient()
send_expo_push = partial(send_expo_push_tokens, client=expo_push_client)
download_spotify_track = partial(
    _download_spotify_track,
    command_runner=command_runner,
)
ensure_goal_song_clip = partial(
    _ensure_goal_song_clip,
    runtime=audio_runtime,
)
prepare_player_song_clip = partial(
    _prepare_player_song_clip,
    runtime=audio_runtime,
)
create_player_song = partial(_create_player_song, jobs=song_jobs)
update_player_song_settings = partial(_update_song_settings, jobs=song_jobs)
enqueue_download_for_player_song = partial(_enqueue_download, jobs=song_jobs)
retry_owned_player_song_download = partial(_retry_owned_song, jobs=song_jobs)


def _web_push_ttl_seconds() -> int:
    return int(getattr(settings, "WEBPUSH_TTL_SECONDS", 3600) or 3600)


def webpush_library_available() -> bool:
    """Return whether the production web-push adapter is installed."""
    return web_push_client.available()


def send_web_push(*, sub: PlayerPushSubscription, payload: WebPushPayload) -> None:
    """Deliver one web notification through the production adapter."""
    if missing_webpush_settings():
        return
    send_to_model_subscription(
        sub=sub,
        payload=payload,
        client=web_push_client,
        ttl_seconds=_web_push_ttl_seconds(),
    )


def send_test_web_pushes(
    *,
    subs: list[PlayerPushSubscription],
    payload: WebPushPayload,
) -> tuple[int, int, list[dict[str, object]]]:
    """Deliver test notifications through the production adapter."""
    return _send_test_payload(
        subs=subs,
        payload=payload,
        client=web_push_client,
        ttl_seconds=_web_push_ttl_seconds(),
    )
