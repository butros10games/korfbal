"""Transport boundary for competition catalogue collection reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from apps.competition.models import HistoricalResource, SyncResource


class TransportError(Exception):
    """Provider transport failed without exposing credentials."""


class ProviderCooldownError(TransportError):
    """The token endpoint asks all provider traffic to wait."""

    def __init__(self, seconds: int) -> None:
        """Retain only the provider's non-sensitive retry delay."""
        super().__init__("Provider cooldown")
        self.seconds = seconds


@dataclass
class FetchResult:
    """Transport-neutral response; no credentials or personal payload logging."""

    status: int
    data: dict[str, Any] | None = None
    etag: str = ""
    retry_after: int = 60


class RequestBudgetError(Exception):
    """A local traffic budget deferred work without a provider failure."""

    def __init__(self, retry_at: datetime) -> None:
        """Expose the next permitted attempt without holding the worker asleep."""
        super().__init__("Provider request budget exhausted")
        self.retry_at = retry_at


class RequestGate(Protocol):
    """Reserve budget before each actual HTTP attempt, including token renewal."""

    def before_request(self) -> None:
        """Reserve and pace one outgoing request."""
        ...


class CompetitionClient(Protocol):
    """Read-only capability used by the discovery service."""

    def fetch(
        self, resource: SyncResource, gate: RequestGate | None = None
    ) -> FetchResult:
        """Fetch one known catalogue resource."""
        ...


class AuthenticationRequiredError(Exception):
    """Refresh credentials expired or were revoked; interactive sign-in is needed."""


class HistoricalClient(Protocol):
    """Read known historical resources without coupling services to HTTP adapters."""

    def fetch(self, resource: HistoricalResource, gate: RequestGate) -> FetchResult:
        """Read one resource within the shared wire request budget."""
        ...

    def close(self) -> None:
        """Release provider connections."""
        ...
