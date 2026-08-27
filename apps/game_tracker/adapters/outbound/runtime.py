"""Production clock, realtime, and Celery adapters for match tracking."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from typing import Protocol, cast

from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.realtime.publisher import publish_match_changed


class _CeleryTask(Protocol):
    def delay(self, *args: object, **kwargs: object) -> object:
        """Dispatch a task immediately."""

    def apply_async(
        self,
        args: tuple[object, ...],
        *,
        countdown: int,
    ) -> object:
        """Dispatch a delayed task."""


def _task(module: str, name: str) -> _CeleryTask:
    tasks = import_module(module)
    return cast(_CeleryTask, getattr(tasks, name))


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
    """Dispatch tracker background work through Celery tasks."""

    @staticmethod
    def _task(name: str) -> _CeleryTask:
        return _task("apps.game_tracker.tasks", name)

    def match_finished(self, *, match_id: str, match_data_id: str) -> None:
        """Schedule post-match processing in the player worker."""
        _task("apps.player.tasks", "handle_match_finished").delay(
            match_id=match_id,
            match_data_id=match_data_id,
        )

    def _dispatch_recompute(
        self,
        task_name: str,
        match_data_id: str,
        countdown_seconds: int,
    ) -> None:
        task = self._task(task_name)
        if countdown_seconds > 0:
            task.apply_async(args=(match_data_id,), countdown=countdown_seconds)
        else:
            task.delay(match_data_id)

    def recompute_impacts(
        self,
        *,
        match_data_id: str,
        countdown_seconds: int = 0,
    ) -> None:
        """Schedule impact recomputation."""
        self._dispatch_recompute(
            "recompute_match_impacts", match_data_id, countdown_seconds
        )

    def recompute_minutes(
        self,
        *,
        match_data_id: str,
        countdown_seconds: int = 0,
    ) -> None:
        """Schedule minutes recomputation."""
        self._dispatch_recompute(
            "recompute_match_minutes", match_data_id, countdown_seconds
        )
