"""HTTP adapter for the observed Sportlink competition API."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import math

import requests

from apps.competition.application.ports import (
    AuthenticationRequiredError,
    FetchResult,
    ProviderCooldownError,
    RequestGate,
    TransportError,
)
from apps.competition.models import SyncResource
from apps.competition.services.resources import ENDPOINTS

from .tokens import TokenStore


BASE_URL = (
    "https://app-sportlinked-production.sportlink.com/entity/common/memberportal/app/"
)


def retry_delay(value: str) -> int:
    """Respect numeric and HTTP-date Retry-After values without truncating waits."""
    if value.isdigit():
        return max(int(value), 60)
    try:
        deadline = parsedate_to_datetime(value)
        return max(math.ceil((deadline - datetime.now(UTC)).total_seconds()), 60)
    except (ValueError, TypeError, OverflowError):
        return 60


class SportlinkClient:
    """Allow only known competition GET endpoints on the fixed provider host."""

    def __init__(
        self, token: str, store: TokenStore | None = None, user_agent: str = ""
    ) -> None:
        """Keep the access token only in the connection session's memory.

        Raises:
            ValueError: The provider requires the originating client User-Agent.

        """
        user_agent = str(store.data["user_agent"]) if store else user_agent
        if not user_agent:
            raise ValueError("Provide the User-Agent from the authorized app session")
        self.store = store
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": user_agent,
            "X-Navajo-Instance": "KNKV",
            "X-Navajo-Locale": "nl",
        })

    def fetch(
        self, resource: SyncResource, gate: RequestGate | None = None
    ) -> FetchResult:
        """Fetch a collection once, using a conditional request where possible.

        Raises:
            ValueError: The response is not a JSON object.
            AuthenticationRequiredError: The renewed session was rejected.

        """
        if self.store and self.store.needs_refresh():
            self._refresh(gate)
        path, parameter, version, _ = ENDPOINTS[resource.kind]
        params = {"v": str(version)}
        if parameter:
            params[parameter] = resource.source_id
        headers = {"X-Navajo-Version": str(version)}
        if resource.etag:
            headers["If-None-Match"] = resource.etag
        response = self._get(
            BASE_URL + path,
            params=params,
            headers=headers,
            gate=gate,
        )
        if response.status_code == requests.codes.unauthorized and self.store:
            self._refresh(gate)
            response = self._get(
                BASE_URL + path, params=params, headers=headers, gate=gate
            )
            if response.status_code == requests.codes.unauthorized:
                raise AuthenticationRequiredError("Sign in again to Sportlink")
        delay = response.headers.get("Retry-After", "60")
        result = FetchResult(
            status=response.status_code,
            etag=response.headers.get("ETag", ""),
            retry_after=retry_delay(delay),
        )
        if response.status_code == requests.codes.ok:
            result.data = response.json()
            if not isinstance(result.data, dict):
                raise ValueError("Expected a competition collection object")
        return result

    def close(self) -> None:
        """Release pooled network connections."""
        self.session.close()

    def _get(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        gate: RequestGate | None = None,
    ) -> requests.Response:
        """Translate provider exceptions to a credential-free application error.

        Raises:
            TransportError: The provider request or decoding failed.

        """
        if gate:
            gate.before_request()
        try:
            return self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=(10, 30),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise TransportError("Sportlink connection failed") from None

    def _refresh(self, gate: RequestGate | None = None) -> None:
        """Renew and persist credentials once without retrying failed refresh grants.

        Raises:
            AuthenticationRequiredError: Refresh is rejected or cannot be persisted.
            TransportError: The token endpoint is temporarily unavailable.
            ProviderCooldownError: Token renewal was rate limited.

        """
        if self.store is None:
            raise AuthenticationRequiredError("No refresh credentials available")
        if gate:
            gate.before_request()
        try:
            response = self.session.post(
                "https://app-sportlinked-production.sportlink.com/oauth/token",
                data=self.store.refresh_form(),
                headers={"Authorization": None},
                timeout=(10, 30),
                allow_redirects=False,
            )
            if response.status_code in {
                requests.codes.bad_request,
                requests.codes.unauthorized,
                requests.codes.forbidden,
            }:
                raise AuthenticationRequiredError("Sign in again to Sportlink")
            if response.status_code == requests.codes.too_many_requests:
                raise ProviderCooldownError(
                    retry_delay(response.headers.get("Retry-After", "60"))
                )
            if response.status_code != requests.codes.ok:
                raise TransportError("Token renewal temporarily unavailable")
            self.store.rotate(response.json())
        except requests.RequestException:
            raise TransportError("Token renewal connection failed") from None
        except (ValueError, KeyError, OSError):
            raise AuthenticationRequiredError(
                "Renewed session could not be saved; sign in again"
            ) from None
        self.session.headers["Authorization"] = f"Bearer {self.store.access_token}"
