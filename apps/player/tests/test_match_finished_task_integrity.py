# ruff: noqa: D103
"""Regression tests for match-finished task transaction safety."""

from unittest.mock import patch

import pytest

from apps.game_tracker.tests.tracker_test_helpers import create_tracker_match
from apps.player.tasks import handle_match_finished


@pytest.mark.django_db
def test_unfinished_match_does_not_consume_idempotency_key() -> None:
    tracker = create_tracker_match(prefix="Early Finished Task")

    with patch("apps.player.tasks.cache.add") as cache_add:
        handle_match_finished(
            match_id=str(tracker.match.id_uuid),
            match_data_id=str(tracker.match_data.id_uuid),
        )

    cache_add.assert_not_called()
