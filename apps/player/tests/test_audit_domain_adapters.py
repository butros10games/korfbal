# ruff: noqa: D103
"""Audit tests for player outbound adapters and management commands."""

from __future__ import annotations

import base64
import subprocess
import sys
from time import monotonic  # nosec B404
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from django.core.management import call_command
from django.db import transaction
from django.test import override_settings
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.player.adapters.outbound.command_runner import SubprocessCommandRunner
from apps.player.adapters.outbound.expo_push import RequestsExpoPushClient
from apps.player.adapters.outbound.song_jobs import CelerySongDownloadDispatcher
from apps.player.adapters.outbound.spotify import RequestsSpotifyClient
from apps.player.adapters.outbound.web_push import PushSession, PyWebPushClient
from apps.player.application.ports import (
    CommandRunOptions,
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
@pytest.mark.django_db
def test_media_dispatch_is_durable_even_with_eager_settings(
    method_name: str, task_name: str
) -> None:
    with override_settings(CELERY_TASK_ALWAYS_EAGER=True):
        getattr(CelerySongDownloadDispatcher(), method_name)("song-id")
    job = BackgroundJob.objects.get()
    assert job.task == f"apps.player.tasks.{task_name}"
    assert job.args == ["song-id"]
    assert job.queue == "media"
    assert job.completed_generation == 0


@pytest.mark.django_db
def test_media_dispatch_rolls_back_with_its_transaction() -> None:
    with transaction.atomic():
        CelerySongDownloadDispatcher().cached_song("song-id")
        transaction.set_rollback(True)
    assert not BackgroundJob.objects.exists()


def test_requests_expo_adapter_uses_provider_contract() -> None:
    response = Mock()
    response.json.return_value = {"data": [{"status": "ok"}]}
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
            subscription={"endpoint": "https://fcm.googleapis.com/sub"},
            data='{"title": "Goal"}',
            ttl_seconds=90,
        )

    assert error.value.status_code == GONE_STATUS_CODE
    assert error.value.__cause__ is provider_error
    provider.assert_called_once_with(
        subscription_info={"endpoint": "https://fcm.googleapis.com/sub"},
        data='{"title": "Goal"}',
        vapid_private_key="private-key",
        vapid_claims={"sub": "mailto:push@example.invalid"},
        ttl=90,
        timeout=10,
        requests_session=ANY,
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


@pytest.mark.parametrize(
    "endpoint", ["http://127.0.0.1/internal", "https://example.invalid/private"]
)
def test_webpush_revalidates_previously_stored_destinations(endpoint: str) -> None:
    """Unsafe legacy subscriptions are rejected before the provider is called."""
    with patch("apps.player.adapters.outbound.web_push.web_push_provider") as provider:
        with pytest.raises(WebPushDeliveryError):
            PyWebPushClient().send(
                subscription={"endpoint": endpoint}, data="{}", ttl_seconds=1
            )
        provider.assert_not_called()


def test_push_transport_never_follows_redirects() -> None:
    """Provider redirects cannot escape the allowlist."""
    with patch("requests.Session.send") as request:
        PushSession().post("https://fcm.googleapis.com/push", allow_redirects=True)
    assert request.call_args.kwargs["allow_redirects"] is False


def test_downloader_timeout_terminates_descendant_with_inherited_output() -> None:
    """A child decoder must not keep pipes open after the parent times out."""
    started = monotonic()
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print('started', flush=True); time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessCommandRunner().run(
            [sys.executable, "-c", script],
            CommandRunOptions(
                check=False,
                capture_output=True,
                text=True,
                timeout=1,
                kill_process_tree=True,
            ),
        )
    maximum_cleanup_seconds = 10
    assert monotonic() - started < maximum_cleanup_seconds


def test_expo_rejection_is_not_mistaken_for_delivery() -> None:
    response = Mock()
    response.json.return_value = {"data": [{"status": "error"}]}
    with (
        patch(
            "apps.player.adapters.outbound.expo_push.requests.post",
            return_value=response,
        ),
        pytest.raises(RuntimeError, match="Expo rejected"),
    ):
        RequestsExpoPushClient().send_messages([{"to": "synthetic"}])
