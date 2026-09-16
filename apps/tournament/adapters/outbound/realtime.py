"""Channels-backed publication for tournament revisions."""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.tournament.adapters.outbound.display_updates import build_display_update
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
            display_frame = None
            try:
                revision, display_frame = build_display_update(tournament_id)
            except Exception:
                # Optional publication optimization must not suppress invalidations.
                logger.exception(
                    "Could not build tournament display update for %s", tournament_id
                )
            async_to_sync(channel_layer.group_send)(
                tournament_group_name(tournament_id),
                {
                    "type": "tournament.changed",
                    "tournament_id": tournament_id,
                    "revision": revision,
                    "display_frame": display_frame,
                },
            )
        except Exception:
            logger.exception(
                "Failed to publish tournament change for %s", tournament_id
            )
