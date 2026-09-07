"""Fixed-host historical reads; app OAuth is never sent to Club.Dataservice."""

from __future__ import annotations

from datetime import date, timedelta
from http import HTTPStatus
from typing import Any

from django.utils import timezone
import requests

from apps.competition.adapters.outbound.sportlink import (
    BASE_URL,
    SportlinkClient,
    retry_delay,
)
from apps.competition.application.ports import FetchResult, RequestGate, TransportError
from apps.competition.models import HistoricalResource
from apps.competition.services.history import DATA_ROW_LIMITS, HistoryUnavailableError


LOOKBACK_WEEKS = 52
DATA_PATHS = {
    "window": "uitslagen",
    "pool_window": "pouleuitslagen",
    "match": "wedstrijd-informatie",
    "standing": "poulestand",
    "members": "poule-indeling",
}


def window_parameters(resource: HistoricalResource) -> tuple[dict, date]:
    """Align an exact date interval to the provider's relative week offset.

    Raises:
        HistoryUnavailableError: The interval exceeds the documented lookback.

    """
    today = timezone.localdate()
    weeks = -(((today - resource.start_date).days + 6) // 7)
    if resource.kind == "window" and weeks < -LOOKBACK_WEEKS:
        raise HistoryUnavailableError("dataservice_52_week_limit")
    wire_start = today + timedelta(weeks=weeks)
    parameters = {
        "weekoffset": weeks,
        "aantaldagen": (resource.end_date - wire_start).days + 1,
        "sorteervolgorde": "datum",
        "eigenwedstrijden": "NEE",
    }
    # Only club results expose a row-limit parameter. Poule results have no
    # documented aantalregels parameter; completeness is reconciled separately.
    if resource.kind == "window":
        parameters["aantalregels"] = DATA_ROW_LIMITS["window"]
    return parameters, wire_start


class HistoryClient:
    """Use app OAuth and an independently configured Dataservice client ID."""

    def __init__(
        self, app: SportlinkClient | None = None, dataservice_id: str = ""
    ) -> None:
        """Keep secrets only in memory and their original provider connection."""
        self.app = app
        self.dataservice_id = dataservice_id
        self.session = requests.Session()

    def close(self) -> None:
        """Release both connections without retaining credentials in checkpoints."""
        self.session.close()
        if self.app:
            self.app.close()

    def fetch(self, resource: HistoricalResource, gate: RequestGate) -> FetchResult:
        """Read one known endpoint and count every attempt and token refresh.

        Raises:
            HistoryUnavailableError: This provider or credential is unavailable.

        """
        if resource.provider == "app":
            return self.fetch_app(resource, gate)
        if resource.provider != "dataservice":
            raise HistoryUnavailableError("archive_has_no_network_endpoint")
        if resource.kind == "pool":
            return FetchResult(status=HTTPStatus.OK, data={})
        if not self.dataservice_id:
            raise HistoryUnavailableError("dataservice_credentials_required")
        return self.fetch_dataservice(resource, gate)

    def fetch_dataservice(
        self, resource: HistoricalResource, gate: RequestGate
    ) -> FetchResult:
        """Fetch date windows or referenced metadata on the Dataservice host.

        Raises:
            TransportError: The provider connection failed.

        """
        params: dict[str, Any] = {"client_id": self.dataservice_id}
        wire_start = resource.start_date
        if resource.kind in {"window", "pool_window"}:
            window, wire_start = window_parameters(resource)
            params.update(window)
            if resource.kind == "pool_window":
                params["poulecode"] = resource.source_id
        else:
            params["wedstrijdcode" if resource.kind == "match" else "poulecode"] = (
                resource.source_id
            )
        headers = {"If-None-Match": resource.etag} if resource.etag else {}
        gate.before_request()
        try:
            response = self.session.get(
                "https://data.sportlink.com/" + DATA_PATHS[resource.kind],
                params=params,
                headers=headers,
                timeout=(10, 30),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise TransportError("Historical provider connection failed") from None
        result = self.response(response)
        if result.status == HTTPStatus.OK:
            result.data = self.dataservice_body(resource, response.json(), wire_start)
        return result

    @staticmethod
    def dataservice_body(
        resource: HistoricalResource, body: object, wire_start: date
    ) -> dict:
        """Validate the provider's response envelope before normalization.

        Raises:
            HistoryUnavailableError: The provider returned an application error.
            ValueError: The endpoint returned an unexpected match envelope.
            TypeError: The collection endpoint did not return a list.

        """
        if isinstance(body, dict) and body.get("error"):
            raise HistoryUnavailableError("dataservice_application_error")
        if resource.kind == "match":
            if not isinstance(body, dict):
                raise ValueError("Expected match details")
            return body
        if not isinstance(body, list):
            raise TypeError("Expected collection")
        return {"rows": body, "wire_start": wire_start}

    def fetch_app(self, resource: HistoricalResource, gate: RequestGate) -> FetchResult:
        """Use verified app endpoints with no speculative date parameters.

        Raises:
            HistoryUnavailableError: The saved app session is unavailable.
            ValueError: The endpoint returned an unexpected envelope.

        """
        if not self.app:
            raise HistoryUnavailableError("app_session_required")
        path, parameter, version = {
            "match": ("match/MatchResultDetails", "PublicMatchId", 8),
            "pool": ("pool/PoolCompetitionData", "PoolId", 2),
        }[resource.kind]
        if self.app.store and self.app.store.needs_refresh():
            self.app._refresh(gate)
        headers = {"X-Navajo-Version": str(version)}
        if resource.etag:
            headers["If-None-Match"] = resource.etag
        params = {parameter: resource.source_id, "v": str(version)}
        response = self.app._get(
            BASE_URL + path, params=params, headers=headers, gate=gate
        )
        if response.status_code == HTTPStatus.UNAUTHORIZED and self.app.store:
            self.app._refresh(gate)
            response = self.app._get(
                BASE_URL + path, params=params, headers=headers, gate=gate
            )
        result = self.response(response)
        if result.status == HTTPStatus.OK:
            result.data = response.json()
            if not isinstance(result.data, dict):
                raise ValueError("Expected historical object")
        return result

    @staticmethod
    def response(response: requests.Response) -> FetchResult:
        """Keep request URLs and provider error text out of persistent state."""
        return FetchResult(
            status=response.status_code,
            etag=response.headers.get("ETag", ""),
            retry_after=retry_delay(response.headers.get("Retry-After", "60")),
        )
