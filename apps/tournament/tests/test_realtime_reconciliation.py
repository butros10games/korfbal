"""Bounded shared reconciliation under many viewers, slow reads and failures."""

import asyncio
from unittest.mock import AsyncMock

from django.db import OperationalError
from django.test import override_settings
import pytest

from apps.tournament import realtime_reconciliation as reconciliation


@pytest.mark.asyncio
@override_settings(KORFBAL_SSE_RECONCILE_SECONDS=0.001)
async def test_viewers_share_reads_and_release_the_last_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One hundred viewers share one read and receive only subscribed revisions."""
    results: asyncio.Queue[dict[str, int]] = asyncio.Queue()
    reader = AsyncMock()

    async def read(ids: tuple[str, ...]) -> dict[str, int]:
        assert set(ids) <= {"one", "two"}
        return await results.get()

    reader.side_effect = read
    monkeypatch.setattr(reconciliation, "read_tournament_revisions", reader)
    streams = [reconciliation.revision_updates(("one",)) for _ in range(100)]
    streams.append(reconciliation.revision_updates(("two",)))
    pending = [asyncio.create_task(anext(stream)) for stream in streams]
    try:
        results.put_nowait({"one": 1, "two": 4})
        values = await asyncio.wait_for(asyncio.gather(*pending), timeout=1)
        assert values == [{"one": 1}] * 100 + [{"two": 4}]
        assert reader.await_args_list[0].args == (("one", "two"),)
        # The reader can have started the next tick, but cannot fan out per viewer.
        maximum_reads = 2
        assert reader.await_count <= maximum_reads
    finally:
        for stream in streams:
            await stream.aclose()
    assert not reconciliation._workers


@pytest.mark.asyncio
@override_settings(KORFBAL_SSE_RECONCILE_SECONDS=0.001)
async def test_slow_viewer_receives_latest_revision_without_blocking_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued stale revisions are replaced while a receiver is stalled."""
    results: asyncio.Queue[dict[str, int]] = asyncio.Queue()

    async def read(ids: tuple[str, ...]) -> dict[str, int]:
        return await results.get()

    monkeypatch.setattr(reconciliation, "read_tournament_revisions", read)
    slow = reconciliation.revision_updates(("one",))
    fast = reconciliation.revision_updates(("one",))
    pending = [asyncio.create_task(anext(stream)) for stream in (slow, fast)]
    try:
        results.put_nowait({"one": 1})
        assert await asyncio.wait_for(asyncio.gather(*pending), 1) == [{"one": 1}] * 2
        for revision in range(2, 6):
            results.put_nowait({"one": revision})
            assert await asyncio.wait_for(anext(fast), 1) == {"one": revision}
        assert await asyncio.wait_for(anext(slow), 1) == {"one": 5}
        await slow.aclose()
        results.put_nowait({"one": 6})
        assert await asyncio.wait_for(anext(fast), 1) == {"one": 6}
    finally:
        await slow.aclose()
        await fast.aclose()
    assert not reconciliation._workers


@pytest.mark.asyncio
@override_settings(KORFBAL_SSE_RECONCILE_SECONDS=0.001)
async def test_database_failure_does_not_disable_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next tick retries a failed read and last-disconnect cancels the worker."""
    reader = AsyncMock(side_effect=[OperationalError("temporary"), {"one": 2}])
    monkeypatch.setattr(reconciliation, "read_tournament_revisions", reader)
    stream = reconciliation.revision_updates(("one",))
    try:
        assert await asyncio.wait_for(anext(stream), 1) == {"one": 2}
        expected_attempts = 2
        assert reader.await_count == expected_attempts
    finally:
        await stream.aclose()
    assert not reconciliation._workers
