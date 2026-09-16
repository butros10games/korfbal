"""Production clock, realtime, and Celery adapters for match tracking."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.game_tracker.application.ports import PublishedLiveStore
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.realtime.publisher import publish_match_changed
from apps.game_tracker.services.public_live import render_published_live
from apps.kwt_common.services.jobs import enqueue


logger = logging.getLogger(__name__)


@dataclass
class ChannelsMatchChangePublisher:
    """Publish tracker revisions through the configured Channels layer."""

    snapshots: PublishedLiveStore
    prepare_snapshot: Callable[[str], None]

    prepare_resources: Callable[[str], None] | None = None
    invalidate_resources: Callable[[str, int], None] | None = None

    read_resources: Callable[..., dict[str, Any]] | None = None

    def schedule_snapshot(self, *, match_id: str, revision: int) -> None:
        """Fence stale reads after commit and durably schedule shared publication."""
        transaction.on_commit(
            lambda: self.snapshots.invalidate(match_id, revision),
            robust=True,
        )
        if self.invalidate_resources is not None:
            invalidate = self.invalidate_resources
            transaction.on_commit(lambda: invalidate(match_id, revision), robust=True)
        enqueue(
            "apps.game_tracker.tasks.publish_public_live_snapshot",
            match_id,
            args=[match_id],
            queue="projections",
        )

    def publish(
        self,
        *,
        match_id: str,
        revision: int,
        resources: Iterable[LiveResource | str],
    ) -> None:
        """Publish one committed revision."""
        resources = list(resources)
        if self.prepare_resources is not None:
            try:
                self.prepare_resources(match_id)
            except Exception:
                logger.exception("Failed to prepare shared public match resources")
        live: dict[str, Any] | None = None
        if LiveResource.LIVE in resources:
            try:
                self.prepare_snapshot(match_id)
                envelope = self.snapshots.get(match_id)
                if envelope is not None and envelope["revision"] == revision:
                    live = render_published_live(envelope)
            except Exception:
                # The write is committed. Durable intent and HTTP recovery remain.
                logger.exception("Failed to prepare committed public snapshot")
        updates = {}
        if self.read_resources is not None:
            try:
                updates = self.read_resources(
                    match_id=match_id, revision=revision, resources=resources
                )
            except Exception:
                logger.exception("Failed to read public updates for publication")
        publish_match_changed(
            match_id=match_id,
            revision=revision,
            resources=resources,
            **({"public_reads": updates} if updates else {}),
            **({"live": live} if live is not None else {}),
        )


class CeleryTrackerJobDispatcher:
    """Persist work inside the tracker transaction without contacting the broker."""

    def match_finished(self, *, match_id: str, match_data_id: str) -> None:
        """Schedule each finished-match lifecycle once."""
        enqueue(
            "apps.player.tasks.handle_match_finished",
            match_data_id,
            kwargs={"match_id": match_id, "match_data_id": match_data_id},
            once=True,
        )

    def recompute_impacts(
        self, *, match_data_id: str, countdown_seconds: int = 0
    ) -> None:
        """Coalesce impact and minutes requests into one projection job."""
        enqueue(
            "apps.game_tracker.tasks.recompute_match_statistics",
            match_data_id,
            args=[match_data_id],
            queue="projections",
            due_at=timezone.now() + timedelta(seconds=countdown_seconds),
        )

    recompute_minutes = recompute_impacts
