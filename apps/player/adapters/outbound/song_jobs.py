"""Celery adapter for player song-download jobs."""

from __future__ import annotations

from importlib import import_module
from typing import Protocol, cast

from django.conf import settings
from kombu.exceptions import OperationalError as KombuOperationalError

from apps.player.application.ports import JobDispatchUnavailableError


class _CeleryTask(Protocol):
    def apply(self, *, args: list[str]) -> object: ...

    def delay(self, song_id: str) -> object: ...


def _task(name: str) -> _CeleryTask:
    tasks = import_module("apps.player.tasks")
    return cast(_CeleryTask, getattr(tasks, name))


def _run_eagerly() -> bool:
    return bool(
        getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)
        or getattr(settings, "TESTING", False)
    )


class CelerySongDownloadDispatcher:
    """Dispatch song downloads through Celery."""

    @staticmethod
    def _dispatch(task_name: str, song_id: str) -> None:
        try:
            task = _task(task_name)
            if _run_eagerly():
                task.apply(args=[song_id])
            else:
                task.delay(song_id)
        except KombuOperationalError as exc:
            raise JobDispatchUnavailableError from exc

    def cached_song(self, song_id: str) -> None:
        """Schedule a cached-song download."""
        self._dispatch("download_cached_song", song_id)

    def player_song(self, song_id: str) -> None:
        """Schedule a legacy player-song download."""
        self._dispatch("download_player_song", song_id)
