"""PyWebPush adapter for browser notifications."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, cast

from django.conf import settings
import requests

from apps.player.application.ports import WebPushDeliveryError
from apps.player.services.push_endpoints import validate_web_push_endpoint


try:
    from pywebpush import (
        WebPushException as _WebPushException,
        webpush as _webpush,
    )
except ImportError:  # pragma: no cover - deployment diagnostic path
    web_push_exception_type: type[Exception] | None = None
    web_push_provider = None
else:
    web_push_exception_type = _WebPushException
    web_push_provider = _webpush


class PushSession(requests.Session):
    """Never follow a push provider redirect to another destination."""

    def send(
        self, request: requests.PreparedRequest, **kwargs: object
    ) -> requests.Response:
        """Validate each request and disable redirects."""
        validate_web_push_endpoint(request.url or "")
        kwargs["allow_redirects"] = False
        return super().send(request, **cast(dict[str, Any], kwargs))


class PyWebPushClient:
    """Deliver notifications using pywebpush and configured VAPID credentials."""

    @staticmethod
    def available() -> bool:
        """Return whether pywebpush is installed."""
        return web_push_provider is not None

    def send(
        self,
        *,
        subscription: dict[str, Any],
        data: str,
        ttl_seconds: int,
    ) -> None:
        """Send one notification.

        Raises:
            RuntimeError: If pywebpush is not installed.
            WebPushDeliveryError: If pywebpush rejects delivery.

        """
        if web_push_provider is None:
            raise RuntimeError(
                "pywebpush is not available in this runtime; cannot send web push"
            )

        try:
            validate_web_push_endpoint(str(subscription.get("endpoint", "")))
        except ValueError as exc:
            raise WebPushDeliveryError(str(exc), status_code=410) from exc

        try:
            with PushSession() as session:
                response = web_push_provider(
                    subscription_info=subscription,
                    data=data,
                    vapid_private_key=str(settings.WEBPUSH_VAPID_PRIVATE_KEY),
                    vapid_claims={"sub": str(settings.WEBPUSH_VAPID_SUBJECT)},
                    ttl=ttl_seconds,
                    timeout=10,
                    requests_session=session,
                )
                if (
                    HTTPStatus.MULTIPLE_CHOICES
                    <= getattr(response, "status_code", HTTPStatus.CREATED)
                    < HTTPStatus.BAD_REQUEST
                ):
                    raise WebPushDeliveryError(
                        "Push redirects are not allowed.", status_code=410
                    )
        except Exception as exc:
            if web_push_exception_type is None or not isinstance(
                exc, web_push_exception_type
            ):
                raise
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            raise WebPushDeliveryError(
                str(exc),
                status_code=status_code if isinstance(status_code, int) else None,
            ) from exc
