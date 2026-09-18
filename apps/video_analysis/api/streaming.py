"""Bounded ASGI streaming for synchronous private object readers."""

from collections.abc import AsyncIterator, Iterator

from asgiref.sync import sync_to_async


def _next(chunks: Iterator[bytes]) -> bytes | None:
    return next(chunks, None)


def _close(chunks: Iterator[bytes]) -> None:
    close = getattr(chunks, "close", None)
    if close:
        close()


async def async_chunks(chunks: Iterator[bytes]) -> AsyncIterator[bytes]:
    """Read one chunk at a time off the event loop and close on disconnect.

    Yields:
        One bounded chunk, without materializing the remaining video.

    """
    try:
        while (chunk := await sync_to_async(_next)(chunks)) is not None:
            yield chunk
    finally:
        await sync_to_async(_close)(chunks)
