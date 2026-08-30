# ruff: noqa: D103
"""Audit tests for player outbound adapters and management commands."""

from __future__ import annotations

import base64
import subprocess  # nosec B404
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import override_settings
from kombu.exceptions import OperationalError as KombuOperationalError
import pytest

from apps.player.adapters.outbound.command_runner import SubprocessCommandRunner
from apps.player.adapters.outbound.expo_push import RequestsExpoPushClient
from apps.player.adapters.outbound.song_jobs import CelerySongDownloadDispatcher
from apps.player.adapters.outbound.spotify import RequestsSpotifyClient
from apps.player.adapters.outbound.web_push import PyWebPushClient
from apps.player.application.ports import (
    CommandRunOptions,
    JobDispatchUnavailableError,
    WebPushDeliveryError,
)
from apps.player.management.commands.generate_vapid_keys import generate_vapid_keypair


GONE_STATUS_CODE = 410
UNCOMPRESSED_POINT_PREFIX = 0x04
P256_PUBLIC_KEY_BYTES = 65
P256_PRIVATE_KEY_BYTES = 32


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def test_subprocess_adapter_forwards_only_explicit_safe_options() -> None:
    completed = subprocess.CompletedProcess(["tool", "arg"], 0, "out", "err")
    options = CommandRunOptions(
        check=True,
        capture_output=True,
        text=True,
        timeout=17,
    )

    with patch(
        "apps.player.adapters.outbound.command_runner.subprocess.run",
        return_value=completed,
    ) as run:
        result = SubprocessCommandRunner().run(("tool", "arg"), options)

    assert result is completed
    run.assert_called_once_with(
        ["tool", "arg"],
        check=True,
        capture_output=True,
        text=True,
        timeout=17,
        shell=False,
    )


@pytest.mark.parametrize(
    ("method_name", "task_name"),
    [
        ("cached_song", "download_cached_song"),
        ("player_song", "download_player_song"),
    ],
)
def test_celery_dispatcher_routes_eager_and_broker_backed_jobs(
    method_name: str,
    task_name: str,
) -> None:
    task = Mock()
    dispatcher = CelerySongDownloadDispatcher()

    with (
        patch(
            "apps.player.adapters.outbound.song_jobs._task",
            return_value=task,
        ) as resolve_task,
        patch(
            "apps.player.adapters.outbound.song_jobs._run_eagerly",
            return_value=True,
        ),
    ):
        getattr(dispatcher, method_name)("song-eager")

    resolve_task.assert_called_once_with(task_name)
    task.apply.assert_called_once_with(args=["song-eager"])
    task.delay.assert_not_called()

    task.reset_mock()
    with (
        patch(
            "apps.player.adapters.outbound.song_jobs._task",
            return_value=task,
        ),
        patch(
            "apps.player.adapters.outbound.song_jobs._run_eagerly",
            return_value=False,
        ),
    ):
        getattr(dispatcher, method_name)("song-delayed")

    task.delay.assert_called_once_with("song-delayed")
    task.apply.assert_not_called()


def test_celery_dispatcher_translates_only_broker_failures() -> None:
    task = Mock()
    task.delay.side_effect = KombuOperationalError("broker unavailable")

    with (
        patch("apps.player.adapters.outbound.song_jobs._task", return_value=task),
        patch(
            "apps.player.adapters.outbound.song_jobs._run_eagerly",
            return_value=False,
        ),
        pytest.raises(JobDispatchUnavailableError) as error,
    ):
        CelerySongDownloadDispatcher().cached_song("song-id")

    assert isinstance(error.value.__cause__, KombuOperationalError)

    task.delay.side_effect = ValueError("programming error")
    with (
        patch("apps.player.adapters.outbound.song_jobs._task", return_value=task),
        patch(
            "apps.player.adapters.outbound.song_jobs._run_eagerly",
            return_value=False,
        ),
        pytest.raises(ValueError, match="programming error"),
    ):
        CelerySongDownloadDispatcher().cached_song("song-id")


def test_requests_expo_adapter_uses_provider_contract() -> None:
    response = Mock()
    messages = [{"to": "ExponentPushToken[value]", "title": "Goal"}]

    with patch(
        "apps.player.adapters.outbound.expo_push.requests.post",
        return_value=response,
    ) as post:
        RequestsExpoPushClient().send_messages(messages)

    post.assert_called_once_with(
        "https://exp.host/--/api/v2/push/send",
        json=messages,
        timeout=10,
    )
    response.raise_for_status.assert_called_once_with()


def test_requests_spotify_adapter_encodes_playback_device() -> None:
    response = Mock()

    with patch(
        "apps.player.adapters.outbound.spotify.requests.put",
        return_value=response,
    ) as put:
        result = RequestsSpotifyClient().put_playback(
            access_token="access-token",
            action="play",
            device_id="device id/&",
            json_body={"uris": ["spotify:track:123"]},
        )

    assert result is response
    put.assert_called_once_with(
        "https://api.spotify.com/v1/me/player/play?device_id=device+id%2F%26",
        headers={
            "Authorization": "Bearer access-token",
            "Content-Type": "application/json",
        },
        json={"uris": ["spotify:track:123"]},
        timeout=10,
    )


@override_settings(
    WEBPUSH_VAPID_PRIVATE_KEY="private-key",
    WEBPUSH_VAPID_SUBJECT="mailto:push@example.invalid",
)
def test_pywebpush_adapter_maps_provider_error_and_preserves_status() -> None:
    class ProviderError(Exception):
        def __init__(self, message: str, *, status_code: int) -> None:
            super().__init__(message)
            self.response = SimpleNamespace(status_code=status_code)

    provider_error = ProviderError("expired", status_code=GONE_STATUS_CODE)
    provider = Mock(side_effect=provider_error)

    with (
        patch(
            "apps.player.adapters.outbound.web_push.web_push_provider",
            provider,
        ),
        patch(
            "apps.player.adapters.outbound.web_push.web_push_exception_type",
            ProviderError,
        ),
        pytest.raises(WebPushDeliveryError) as error,
    ):
        PyWebPushClient().send(
            subscription={"endpoint": "https://push.example.invalid/sub"},
            data='{"title": "Goal"}',
            ttl_seconds=90,
        )

    assert error.value.status_code == GONE_STATUS_CODE
    assert error.value.__cause__ is provider_error
    provider.assert_called_once_with(
        subscription_info={"endpoint": "https://push.example.invalid/sub"},
        data='{"title": "Goal"}',
        vapid_private_key="private-key",
        vapid_claims={"sub": "mailto:push@example.invalid"},
        ttl=90,
    )


def test_pywebpush_adapter_rejects_missing_runtime() -> None:
    with (
        patch("apps.player.adapters.outbound.web_push.web_push_provider", None),
        pytest.raises(RuntimeError, match="pywebpush is not available"),
    ):
        PyWebPushClient().send(
            subscription={},
            data="{}",
            ttl_seconds=60,
        )


def test_vapid_key_generation_produces_distinct_p256_material() -> None:
    first_public, first_private = generate_vapid_keypair()
    second_public, second_private = generate_vapid_keypair()

    assert "=" not in first_public
    assert "=" not in first_private
    assert _decode_base64url(first_public)[0] == UNCOMPRESSED_POINT_PREFIX
    assert len(_decode_base64url(first_public)) == P256_PUBLIC_KEY_BYTES
    assert len(_decode_base64url(first_private)) == P256_PRIVATE_KEY_BYTES
    assert (first_public, first_private) != (second_public, second_private)


def test_generate_vapid_keys_command_prints_configurable_subject(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "apps.player.management.commands.generate_vapid_keys.generate_vapid_keypair",
        return_value=("public", "private"),
    ):
        call_command("generate_vapid_keys", subject="https://push.example.invalid")

    output = capsys.readouterr().out
    assert "WEBPUSH_VAPID_PUBLIC_KEY=public" in output
    assert "WEBPUSH_VAPID_PRIVATE_KEY=private" in output
    assert "WEBPUSH_VAPID_SUBJECT=https://push.example.invalid" in output
    assert "WEBPUSH_TTL_SECONDS=3600" in output
