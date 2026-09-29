"""Match clock labels used by public match summaries."""

import pytest

from apps.game_tracker.domain.match_clock import format_part_length


@pytest.mark.parametrize(
    ("seconds", "label"),
    [(3599, "59:59"), (1500, "25:00"), (0, "00:00"), (61.9, "01:01")],
)
def test_part_length_is_formatted_as_zero_padded_minutes_and_seconds(
    seconds: float, label: str
) -> None:
    """Durations are formatted as zero-padded minutes and seconds."""
    assert format_part_length(seconds) == label
