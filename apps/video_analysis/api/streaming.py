"""Private media responses and bounded ASGI streaming for object readers."""

from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus
import mimetypes

from asgiref.sync import sync_to_async
from django.core.handlers.asgi import ASGIRequest
from django.http import (
    HttpRequest,
    HttpResponseBase,
    HttpResponseRedirect,
    JsonResponse,
    StreamingHttpResponse,
)

from apps.video_analysis.engine.server import parse_range
from apps.video_analysis.engine.store import Store


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


def stored_response(
    request: HttpRequest, store: Store, relative: str
) -> HttpResponseBase:
    """Prefer direct private S3 delivery; internal legacy MinIO stays proxied."""
    files = getattr(store, "files", None)
    url = files.media_url(relative) if files else None
    if url:
        response = HttpResponseRedirect(url, preserve_request=True)
        response["Referrer-Policy"] = "no-referrer"
        return response
    return stream_response(request, store, relative)


def stream_response(
    request: HttpRequest, store: Store, relative: str
) -> HttpResponseBase:
    """Stream authorized ranges without materializing any local file."""
    size = store.media_size(relative)
    start, end = 0, size - 1
    status = 200
    if request.headers.get("Range"):
        try:
            start, end = parse_range(request.headers["Range"], size)
        except ValueError:
            response = JsonResponse({"error": "Invalid range"}, status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        status = 206

    chunks = store.media_chunks(relative, start, end)
    response = StreamingHttpResponse(
        async_chunks(chunks) if isinstance(request, ASGIRequest) else chunks,
        status=status,
        content_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
    )
    response["Content-Length"] = str(end - start + 1)
    response["Accept-Ranges"] = "bytes"
    if status == HTTPStatus.PARTIAL_CONTENT:
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    return response
