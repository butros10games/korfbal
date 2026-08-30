"""Architecture tests for event-editor input adapters."""

import pytest

from apps.schedule.api.serializers import (
    PauseWriteSerializer,
    PlayerChangeWriteSerializer,
    ShotWriteSerializer,
    TimeoutWriteSerializer,
)


@pytest.mark.parametrize(
    "serializer_type",
    [
        ShotWriteSerializer,
        PlayerChangeWriteSerializer,
        PauseWriteSerializer,
        TimeoutWriteSerializer,
    ],
)
def test_event_editor_serializers_are_input_adapters_only(
    serializer_type: type[object],
) -> None:
    """Keep persistence out of command-parsing serializers."""
    assert "create" not in serializer_type.__dict__
    assert "update" not in serializer_type.__dict__
