"""PyWebPush adapter for browser notifications."""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.player.application.ports import WebPushDeliveryError


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
            web_push_provider(
                subscription_info=subscription,
                data=data,
                vapid_private_key=str(settings.WEBPUSH_VAPID_PRIVATE_KEY),
                vapid_claims={"sub": str(settings.WEBPUSH_VAPID_SUBJECT)},
                ttl=ttl_seconds,
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
