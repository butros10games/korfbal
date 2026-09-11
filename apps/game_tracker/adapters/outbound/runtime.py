"""Production clock, realtime, and Celery adapters for match tracking."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from django.utils import timezone

from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.realtime.publisher import publish_match_changed
from apps.kwt_common.services.jobs import enqueue


class ChannelsMatchChangePublisher:
    """Publish tracker revisions through the configured Channels layer."""

    def publish(
        self,
        *,
        match_id: str,
        revision: int,
        resources: Iterable[LiveResource | str],
    ) -> None:
        """Publish one committed revision."""
        publish_match_changed(
            match_id=match_id,
            revision=revision,
            resources=resources,
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
