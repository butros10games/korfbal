"""HTTPS-only transport with no redirects or credential-bearing error text."""

from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPException, HTTPResponse, HTTPSConnection
from urllib.parse import urlsplit


@contextmanager
def request(
    url: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Iterator[HTTPResponse]:
    """Open an encrypted request; callers handle status codes explicitly.

    Yields:
        The response; its connection closes when the context exits.

    Raises:
        ValueError: If the endpoint is not an HTTPS URL without user credentials.
        OSError: If HTTP transport fails.

    """
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("Expected HTTPS endpoint without embedded credentials")
    connection = HTTPSConnection(parts.hostname, parts.port, timeout=60)
    try:
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        connection.request(method, path, body=data, headers=headers or {})
        yield connection.getresponse()
    except HTTPException as error:
        raise OSError(type(error).__name__) from None
    finally:
        connection.close()
