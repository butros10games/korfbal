"""Best-effort Valkey publication for committed match changes."""

from __future__ import annotations

from collections.abc import Iterable
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .contracts import LiveResource
from .metrics import SSE_PUBLICATIONS


logger = logging.getLogger(__name__)


def match_group_name(match_id: str) -> str:
    """Return the stable Channels group for a match UUID."""
    return f"korfbal.match.{match_id}"


def publish_match_changed(
    *,
    match_id: str,
    revision: int,
    resources: Iterable[LiveResource | str],
) -> None:
    """Publish one committed change without risking the database operation."""
    resource_values = sorted({str(resource) for resource in resources})
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            SSE_PUBLICATIONS.labels(result="unavailable").inc()
            logger.warning("No channel layer configured for match %s", match_id)
            return
        async_to_sync(channel_layer.group_send)(
            match_group_name(match_id),
            {
                "type": "match.changed",
                "match_id": match_id,
                "revision": revision,
                "resources": resource_values,
            },
        )
        SSE_PUBLICATIONS.labels(result="success").inc()
    except Exception:
        SSE_PUBLICATIONS.labels(result="failure").inc()
        logger.exception(
            "Failed to publish match.changed for match=%s revision=%s",
            match_id,
            revision,
        )
