"""Channels-backed publication for tournament revisions."""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.tournament.realtime_contracts import tournament_group_name


logger = logging.getLogger(__name__)


class ChannelsTournamentChangePublisher:
    """Publish tournament revisions through the configured Channels layer."""

    def publish(self, *, tournament_id: str, revision: int) -> None:
        """Publish a committed revision without risking the database write."""
        try:
            channel_layer = get_channel_layer()
            if channel_layer is None:
                return
            async_to_sync(channel_layer.group_send)(
                tournament_group_name(tournament_id),
                {
                    "type": "tournament.changed",
                    "tournament_id": tournament_id,
                    "revision": revision,
                },
            )
        except Exception:
            logger.exception(
                "Failed to publish tournament change for %s", tournament_id
            )
