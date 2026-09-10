"""Share durable revision reads between streams on one ASGI event loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import logging

from channels.db import database_sync_to_async
from django.conf import settings
from django.db import DatabaseError

from apps.tournament.models import Tournament


logger = logging.getLogger(__name__)
_QUERY_BATCH_SIZE = 500
_workers: dict[asyncio.AbstractEventLoop, _Reconciler] = {}


@database_sync_to_async
def read_tournament_revisions(tournament_ids: tuple[str, ...]) -> dict[str, int]:
    """Read current revisions in bounded database parameter batches."""
    revisions = {}
    for offset in range(0, len(tournament_ids), _QUERY_BATCH_SIZE):
        revisions.update({
            str(tournament_id): revision
            for tournament_id, revision in Tournament.objects
            .filter(id_uuid__in=tournament_ids[offset : offset + _QUERY_BATCH_SIZE])
            .order_by()
            .values_list("id_uuid", "live_revision")
        })
    return revisions


class _Reconciler:
    def __init__(self) -> None:
        self.subscribers: dict[asyncio.Queue[dict[str, int]], tuple[str, ...]] = {}
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        while True:
            await asyncio.sleep(settings.KORFBAL_SSE_RECONCILE_SECONDS)
            tournament_ids = tuple(
                dict.fromkeys(
                    tournament_id
                    for ids in self.subscribers.values()
                    for tournament_id in ids
                )
            )
            try:
                revisions = await read_tournament_revisions(tournament_ids)
            except DatabaseError:
                # A transient database outage must not permanently disable recovery.
                logger.exception("Tournament revision reconciliation failed")
                continue
            for queue, ids in self.subscribers.items():
                # A slow receiver retains only the newest revision for its own
                # subscriptions; it cannot block another stream or grow a backlog.
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait({
                    key: revisions[key] for key in ids if key in revisions
                })


async def revision_updates(
    tournament_ids: tuple[str, ...],
) -> AsyncGenerator[dict[str, int], None]:
    """Release the shared worker after its last subscriber disconnects.

    Yields:
        Current revisions for this stream's subscribed tournaments.

    """
    loop = asyncio.get_running_loop()
    worker = _workers.get(loop)
    if worker is None:
        worker = _workers[loop] = _Reconciler()
    queue: asyncio.Queue[dict[str, int]] = asyncio.Queue(maxsize=1)
    worker.subscribers[queue] = tournament_ids
    try:
        while True:
            yield await queue.get()
    finally:
        del worker.subscribers[queue]
        if not worker.subscribers:
            del _workers[loop]
            worker.task.cancel()
            await asyncio.gather(worker.task, return_exceptions=True)
