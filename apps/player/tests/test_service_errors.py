"""Service errors must survive traceback handling by context managers."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from apps.player.services.goal_song import GoalSongPayloadError, GoalSongSelectionError
from apps.player.services.push_notifications import PushTestConfigurationError
from apps.player.services.spotify import (
    SpotifyAccessError,
    SpotifyInputError,
    SpotifyPlaybackError,
)


@contextmanager
def service_boundary() -> Iterator[None]:
    """Exercise the traceback assignment performed by generator context managers."""
    yield


@pytest.mark.parametrize(
    "error",
    [
        GoalSongPayloadError("Invalid payload"),
        GoalSongSelectionError("Invalid selection", missing=["song"]),
        PushTestConfigurationError("Missing configuration", missing=["vapid"]),
        SpotifyInputError("Missing track"),
        SpotifyAccessError("Not connected"),
        SpotifyPlaybackError("no_active_device", "No device", conflict=True),
    ],
    ids=lambda error: type(error).__name__,
)
def test_service_error_survives_context_manager(error: Exception) -> None:
    """Propagate the original domain error instead of masking it during unwinding."""
    with pytest.raises(type(error)) as caught, service_boundary():
        raise error

    assert caught.value is error
    assert caught.value.__traceback__ is not None
