"""Fixed-host KNKV form transport; credentials stay in the worker session."""

from typing import Any

import requests

from apps.competition.application.match_forms import MatchFormError
from apps.competition.application.ports import ProviderCooldownError, RequestGate

from .sportlink import BASE_URL, SportlinkClient, retry_delay


FORM_PATHS = {
    "players": ("matchform/MatchFormTeamPersonsForm", 4),
    "events": ("matchform/MatchFormMatchEventsForm", 3),
    "info": ("matchform/MatchFormInfo", 6),
    "details": ("match/MatchResultDetails", 8),
    "search": ("matchform/SearchMatchFormTeamPerson", 3),
}


class SportlinkMatchForms:
    """Reuse OAuth renewal and the provider-wide traffic lease."""

    def __init__(self, client: SportlinkClient, gate: RequestGate) -> None:
        """Bind a session and its already acquired traffic lease."""
        self.client = client
        self.gate = gate
        self.etags: dict[tuple[str, str, bool | None], str] = {}

    def _request(
        self,
        method: str,
        resource: str,
        match_id: str,
        home: bool | None,
        *,
        body: dict | None = None,
        **extra_query: str,
    ) -> dict[str, Any]:
        path, version = FORM_PATHS[resource]
        params = {"PublicMatchId": match_id, "v": str(version)}
        if home is not None:
            params["IsHome"] = str(home).lower()
        params.update(extra_query)
        headers = {"X-Navajo-Version": str(version)}
        key = (resource, match_id, home)
        if method == "PUT" and self.etags.get(key):
            headers["If-Match"] = self.etags[key]
        if self.client.store and self.client.store.needs_refresh():
            self.client._refresh(self.gate)
        for attempt in range(2):
            self.gate.before_request()
            try:
                response = self.client.session.request(
                    method,
                    BASE_URL + path,
                    params=params,
                    headers=headers,
                    json=body,
                    timeout=(10, 30),
                    allow_redirects=False,
                )
            except requests.RequestException:
                raise MatchFormError("connection_failed") from None
            if (
                response.status_code == requests.codes.unauthorized
                and attempt == 0
                and self.client.store
            ):
                self.client._refresh(self.gate)
                continue
            break
        data = _response_body(response, resource, match_id)
        if method == "GET":
            self.etags[key] = response.headers.get("ETag", "")
        return data

    def find_player(self, match_id: str, home: bool, person_id: str) -> dict[str, Any]:
        """Never guess a roster identity by a display-name match.

        Raises:
            MatchFormError: The response does not identify exactly one matching player.

        """
        data = self._request(
            "GET",
            "search",
            match_id,
            home,
            Query=person_id,
            IsPlayer="true",
            LimitSearch="false",
        )
        rows = data.get("MatchFormTeamPerson")
        if not isinstance(rows, list):
            raise MatchFormError("invalid_response")
        found = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("PersonId") == person_id
            and (row.get("TeamPersonFunction") or {}).get("RoleId") == "PLAYER_DEFAULT"
        ]
        if len(found) != 1:
            raise MatchFormError("players_not_linked")
        return found[0]

    def read(
        self, resource: str, match_id: str, *, home: bool | None = None
    ) -> dict[str, Any]:
        """Read a form without caching private bodies."""
        return self._request("GET", resource, match_id, home)

    def replace(
        self,
        resource: str,
        match_id: str,
        original: dict[str, Any],
        updated: dict[str, Any],
        *,
        home: bool | None = None,
    ) -> dict[str, Any]:
        """Preflight changes, preserve ETags, and read back the committed form.

        Raises:
            MatchFormError: The resource is read-only or the form changed since reading.

        """
        if resource not in {"players", "events"}:
            raise MatchFormError("invalid_action")
        current = self.read(resource, match_id, home=home)
        if current != original:
            raise MatchFormError("knkv_changed")
        if updated != original:
            self._request("PUT", resource, match_id, home, body=updated)
        return self.read(resource, match_id, home=home)


def _response_body(
    response: requests.Response, resource: str, match_id: str
) -> dict[str, Any]:
    """Decode a bounded response without exposing provider error bodies.

    Raises:
        MatchFormError: KNKV returned an error, malformed body, or another match.
        ProviderCooldownError: The provider requested a cooldown.

    """
    if response.status_code == requests.codes.too_many_requests:
        raise ProviderCooldownError(
            retry_delay(response.headers.get("Retry-After", "60"))
        )
    if response.status_code in {401, 403}:
        raise MatchFormError("knkv_access_denied")
    if response.status_code in {409, 412}:
        raise MatchFormError("knkv_changed")
    if response.status_code != requests.codes.ok:
        raise MatchFormError("knkv_unavailable")
    try:
        data = response.json()
    except ValueError:
        raise MatchFormError("invalid_response") from None
    if (
        not isinstance(data, dict)
        or data.get("Error")
        or (resource != "search" and data.get("PublicMatchId") != match_id)
    ):
        raise MatchFormError("invalid_response")
    return data
