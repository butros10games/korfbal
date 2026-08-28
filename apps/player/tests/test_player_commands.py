# ruff: noqa: D103
"""Direct contract tests for player application commands."""

from __future__ import annotations

from unittest.mock import Mock

from django.test import override_settings
import pytest

from apps.game_tracker.tests.tracker_test_helpers import create_tracker_player
from apps.player.models import Player, PlayerPushSubscription, PlayerSong
from apps.player.services.player_settings import (
    player_privacy_settings,
    update_player_privacy_settings,
)
from apps.player.services.player_songs import (
    PlayerSongNotFoundError,
    delete_owned_player_song,
)
from apps.player.services.push_notifications import send_test_push_notification
from apps.player.services.push_subscriptions import (
    PushSubscriptionNotFoundError,
    deactivate_push_subscription,
    register_push_subscription,
)


@pytest.mark.django_db
def test_push_subscription_commands_preserve_endpoint_ownership() -> None:
    first_player = create_tracker_player(username="push-command-first")
    second_player = create_tracker_player(username="push-command-second")
    payload = {
        "endpoint": "https://example.com/push/command",
        "keys": {"p256dh": "abc", "auth": "def"},
    }

    created = register_push_subscription(
        user_id=first_player.user_id,
        subscription=payload,
        platform="web",
        user_agent="first",
    )
    refreshed = register_push_subscription(
        user_id=second_player.user_id,
        subscription=payload,
        platform="web",
        user_agent="second",
    )

    assert created.created is True
    assert refreshed.created is False
    assert refreshed.subscription.user_id == second_player.user_id
    assert refreshed.subscription.user_agent == "second"

    with pytest.raises(PushSubscriptionNotFoundError):
        deactivate_push_subscription(
            user_id=first_player.user_id,
            endpoint=payload["endpoint"],
            subscription_id=None,
        )

    deactivate_push_subscription(
        user_id=second_player.user_id,
        endpoint=payload["endpoint"],
        subscription_id=None,
    )
    refreshed.subscription.refresh_from_db()
    assert refreshed.subscription.is_active is False


@pytest.mark.django_db
def test_privacy_command_updates_only_supplied_settings() -> None:
    player = create_tracker_player(username="privacy-command")

    update_player_privacy_settings(
        player=player,
        changes={"stats_visibility": Player.Visibility.CLUB},
    )

    player.refresh_from_db()
    assert player_privacy_settings(player) == {
        "profile_picture_visibility": Player.Visibility.PUBLIC,
        "stats_visibility": Player.Visibility.CLUB,
        "teams_visibility": Player.Visibility.PUBLIC,
    }


@pytest.mark.django_db
def test_owned_song_command_rejects_another_players_song() -> None:
    owner = create_tracker_player(username="song-command-owner")
    other = create_tracker_player(username="song-command-other")
    song = PlayerSong.objects.create(player=owner)

    with pytest.raises(PlayerSongNotFoundError):
        delete_owned_player_song(player=other, song_id=str(song.id_uuid))

    assert PlayerSong.objects.filter(id_uuid=song.id_uuid).exists()


@pytest.mark.django_db
@override_settings(
    WEBPUSH_VAPID_PUBLIC_KEY="public",
    WEBPUSH_VAPID_PRIVATE_KEY="private",
    WEBPUSH_VAPID_SUBJECT="mailto:test@example.com",
)
def test_test_push_command_owns_provider_orchestration() -> None:
    player = create_tracker_player(username="test-push-command")
    subscription = PlayerPushSubscription.objects.create(
        user_id=player.user_id,
        endpoint="https://example.com/push/test-command",
        subscription={
            "endpoint": "https://example.com/push/test-command",
            "keys": {"p256dh": "abc", "auth": "def"},
        },
    )
    send_pushes = Mock(return_value=(1, 0, []))

    result = send_test_push_notification(
        user_id=player.user_id,
        webpush_available=lambda: True,
        send_pushes=send_pushes,
    )

    assert result.total == 1
    assert result.sent == 1
    assert result.failed == 0
    assert send_pushes.call_args.kwargs["subs"] == [subscription]
    assert send_pushes.call_args.kwargs["payload"].title == "Test pushmelding"
