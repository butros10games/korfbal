"""APNs HTTP/2 client for Live Activity pushes."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import threading
import time
from typing import Any

from django.conf import settings
import httpx
import jwt

from apps.player.application.ports import LiveActivityDeliveryError


logger = logging.getLogger(__name__)

# Apple accepts provider tokens for an hour; refresh well inside that window.
PROVIDER_TOKEN_LIFETIME_SECONDS = 45 * 60
# Reasons for which APNs will never accept this device token again.
PERMANENT_REASONS = frozenset({
    "BadDeviceToken",
    "Unregistered",
    "DeviceTokenNotForTopic",
    "ExpiredToken",
})


@dataclass(slots=True)
class ApnsLiveActivityClient:
    """Send ActivityKit pushes with an ES256 provider token."""

    team_id: str
    key_id: str
    private_key: str
    bundle_id: str
    sandbox: bool = False
    timeout_seconds: float = 10.0
    transport: httpx.BaseTransport | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _token: str = field(default="", repr=False)
    _token_issued_at: float = field(default=0.0, repr=False)
    _http: httpx.Client | None = field(default=None, repr=False)

    @classmethod
    def from_settings(cls) -> ApnsLiveActivityClient:
        """Build the production client from Django settings."""
        return cls(
            team_id=str(getattr(settings, "APNS_TEAM_ID", "") or ""),
            key_id=str(getattr(settings, "APNS_KEY_ID", "") or ""),
            private_key=str(getattr(settings, "APNS_PRIVATE_KEY", "") or "")
            .replace("\\n", "\n")
            .strip(),
            bundle_id=str(getattr(settings, "APNS_BUNDLE_ID", "") or ""),
            sandbox=bool(getattr(settings, "APNS_USE_SANDBOX", False)),
        )

    @property
    def configured(self) -> bool:
        """Return whether every credential needed for a push is present."""
        return all((self.team_id, self.key_id, self.private_key, self.bundle_id))

    @property
    def base_url(self) -> str:
        """Return the APNs host for the configured environment."""
        host = "api.sandbox.push.apple.com" if self.sandbox else "api.push.apple.com"
        return f"https://{host}"

    def _provider_token(self) -> str:
        with self._lock:
            now = time.time()
            if (
                not self._token
                or now - self._token_issued_at > PROVIDER_TOKEN_LIFETIME_SECONDS
            ):
                self._token = jwt.encode(
                    {"iss": self.team_id, "iat": int(now)},
                    self.private_key,
                    algorithm="ES256",
                    headers={"kid": self.key_id},
                )
                self._token_issued_at = now
            return self._token

    def _client(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                self._http = httpx.Client(
                    base_url=self.base_url,
                    http2=True,
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                )
            return self._http

    def send(self, *, token: str, payload: dict[str, Any]) -> None:
        """Deliver one Live Activity payload to one activity token.

        Raises:
            LiveActivityDeliveryError: APNs rejected the request.

        """
        if not self.configured:
            raise LiveActivityDeliveryError(
                status_code=0, reason="NotConfigured", permanent=False
            )
        response = self._client().post(
            f"/3/device/{token}",
            content=json.dumps(payload).encode(),
            headers={
                "authorization": f"bearer {self._provider_token()}",
                "apns-push-type": "liveactivity",
                "apns-topic": f"{self.bundle_id}.push-type.liveactivity",
                "apns-priority": "10",
                "content-type": "application/json",
            },
        )
        if response.status_code == httpx.codes.OK:
            return
        reason = ""
        try:
            reason = str(response.json().get("reason") or "")
        except ValueError:
            reason = response.text[:80]
        if reason == "ExpiredProviderToken":
            with self._lock:
                self._token = ""
        raise LiveActivityDeliveryError(
            status_code=response.status_code,
            reason=reason,
            permanent=reason in PERMANENT_REASONS,
        )
