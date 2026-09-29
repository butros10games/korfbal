"""Scope session and CSRF cookies to the domain the request arrived on.

The deployment answers on several parent domains (``korfbal.butrosgroot.com``
and ``korfconnect.nl``), but Django only supports one ``SESSION_COOKIE_DOMAIN``
and ``CSRF_COOKIE_DOMAIN``. Browsers silently reject a cookie whose domain does
not cover the responding host, so sign-in would fail on every other domain.
Cookies scoped to one of ``KORFBAL_COOKIE_DOMAINS`` are therefore re-scoped to
the configured domain that matches the request host.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
import inspect
from typing import cast

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import HttpRequest, HttpResponseBase
from django.http.request import split_domain_port
from django.utils.decorators import sync_and_async_middleware


SyncGetResponse = Callable[[HttpRequest], HttpResponseBase]
AsyncGetResponse = Callable[[HttpRequest], Awaitable[HttpResponseBase]]
GetResponse = SyncGetResponse | AsyncGetResponse


def cookie_domain_for_host(host: str, domains: Iterable[str]) -> str | None:
    """Return the configured cookie domain that covers ``host``, if any."""
    hostname, _port = split_domain_port(host)
    for domain in domains:
        bare = domain.lstrip(".").lower()
        if bare and (hostname == bare or hostname.endswith(f".{bare}")):
            return domain
    return None


def _rescope_cookies(request: HttpRequest, response: HttpResponseBase) -> None:
    domains = tuple(settings.KORFBAL_COOKIE_DOMAINS)
    if not domains or not response.cookies:
        return
    try:
        host = request.get_host()
    except DisallowedHost:
        return
    target = cookie_domain_for_host(host, domains)
    if target is None:
        return
    for morsel in response.cookies.values():
        if morsel["domain"] in domains and morsel["domain"] != target:
            morsel["domain"] = target


@sync_and_async_middleware
def cookie_domain_middleware(get_response: GetResponse) -> GetResponse:
    """Re-scope configured cookie domains to the requesting domain."""
    if inspect.iscoroutinefunction(get_response):
        async_get_response = cast(AsyncGetResponse, get_response)

        async def async_middleware(request: HttpRequest) -> HttpResponseBase:
            response = await async_get_response(request)
            _rescope_cookies(request, response)
            return response

        return async_middleware

    sync_get_response = cast(SyncGetResponse, get_response)

    def middleware(request: HttpRequest) -> HttpResponseBase:
        response = sync_get_response(request)
        _rescope_cookies(request, response)
        return response

    return middleware
