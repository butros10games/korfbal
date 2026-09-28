"""Resolve the real client address behind the reverse proxy.

Per-IP rate limits (login, 2FA, registration, password reset) key on
``REMOTE_ADDR``. Behind a proxy that is the proxy's own address, which would make
every limit global: a few requests per minute from anyone could block all
sign-ins. Forwarded headers are only honoured from ``KORFBAL_TRUSTED_PROXIES``;
from any other peer they are removed so a direct caller cannot spoof its address
or ``X-Forwarded-Proto``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from functools import lru_cache
import inspect
import ipaddress
from typing import Any, cast

from django.conf import settings
from django.http import HttpRequest, HttpResponseBase
from django.utils.decorators import sync_and_async_middleware


IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
FORWARDED_META_KEYS = (
    "HTTP_X_REAL_IP",
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_FORWARDED_PROTO",
    "HTTP_X_FORWARDED_HOST",
    "HTTP_X_FORWARDED_PORT",
    "HTTP_FORWARDED",
)
PROXY_ADDR_META_KEY = "KORFBAL_PROXY_ADDR"
SyncGetResponse = Callable[[HttpRequest], HttpResponseBase]
AsyncGetResponse = Callable[[HttpRequest], Awaitable[HttpResponseBase]]
GetResponse = SyncGetResponse | AsyncGetResponse


@lru_cache(maxsize=4)
def parse_networks(values: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    """Parse configured proxy CIDRs once per distinct setting value."""
    return tuple(ipaddress.ip_network(value, strict=False) for value in values)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _is_trusted(value: str, networks: Iterable[IPNetwork]) -> bool:
    address = _parse_ip(value)
    return address is not None and any(address in network for network in networks)


def resolve_client_ip(meta: dict[str, Any], networks: tuple[IPNetwork, ...]) -> str:
    """Return the first address not owned by a trusted proxy.

    ``X-Real-IP`` is set by the edge proxy and wins. Otherwise walk
    ``X-Forwarded-For`` from the right, skipping trusted hops, because only the
    entries appended by our own proxies are trustworthy.
    """
    remote = str(meta.get("REMOTE_ADDR") or "")
    real_ip = _parse_ip(str(meta.get("HTTP_X_REAL_IP") or ""))
    if real_ip is not None:
        return str(real_ip)
    hops = [
        hop.strip() for hop in str(meta.get("HTTP_X_FORWARDED_FOR") or "").split(",")
    ]
    for hop in reversed([hop for hop in hops if hop]):
        address = _parse_ip(hop)
        if address is None:
            break
        if not any(address in network for network in networks):
            return str(address)
    return remote


def _normalize_peer(request: HttpRequest) -> None:
    networks = parse_networks(tuple(settings.KORFBAL_TRUSTED_PROXIES))
    meta = request.META
    remote = str(meta.get("REMOTE_ADDR") or "")
    if _is_trusted(remote, networks):
        meta[PROXY_ADDR_META_KEY] = remote
        meta["REMOTE_ADDR"] = resolve_client_ip(meta, networks)
        return
    for key in FORWARDED_META_KEYS:
        meta.pop(key, None)


@sync_and_async_middleware
def trusted_proxy_middleware(get_response: GetResponse) -> GetResponse:
    """Rewrite ``REMOTE_ADDR`` from a trusted proxy; strip spoofed headers."""
    if inspect.iscoroutinefunction(get_response):
        async_get_response = cast(AsyncGetResponse, get_response)

        async def async_middleware(request: HttpRequest) -> HttpResponseBase:
            _normalize_peer(request)
            return await async_get_response(request)

        return async_middleware

    sync_get_response = cast(SyncGetResponse, get_response)

    def middleware(request: HttpRequest) -> HttpResponseBase:
        _normalize_peer(request)
        return sync_get_response(request)

    return middleware
