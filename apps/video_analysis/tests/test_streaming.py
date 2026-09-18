"""ASGI media must not buffer entire match recordings."""

import asyncio
from collections.abc import Iterator
import io
from unittest.mock import MagicMock, patch

from asgiref.sync import async_to_sync
from django.core.handlers.asgi import ASGIRequest
from django.http import StreamingHttpResponse

from apps.video_analysis.api.streaming import async_chunks
from apps.video_analysis.api.views import media


def test_async_stream_is_lazy_and_closes_on_disconnect() -> None:
    """An unbounded input yields promptly and releases its object body on close."""
    reads = []
    closed = []

    def source() -> Iterator[bytes]:
        try:
            while True:
                reads.append(1)
                yield b"chunk"
        finally:
            closed.append(True)

    async def consume() -> None:
        stream = async_chunks(source())
        assert await asyncio.wait_for(anext(stream), 2) == b"chunk"
        assert len(reads) == 1
        await stream.aclose()
        assert closed == [True]

    async_to_sync(consume)()


def test_asgi_media_response_uses_async_iterator() -> None:
    """The actual media response selects bounded async streaming under ASGI."""
    request = ASGIRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/video-analysis/media",
            "query_string": b"path=video.mp4",
            "headers": [],
        },
        io.BytesIO(),
    )
    store = MagicMock()
    store.media_size.return_value = 4_000_000_000
    store.media_chunks.return_value = iter([b"first", b"second"])
    with patch("apps.video_analysis.api.views.registered_media", return_value=True):
        response = media(request, store, MagicMock())
    assert isinstance(response, StreamingHttpResponse)
    assert response.is_async
    assert response["Content-Length"] == "4000000000"
    store.read.assert_not_called()

    async def consume() -> None:
        assert b"".join([chunk async for chunk in response]) == b"firstsecond"

    async_to_sync(consume)()
