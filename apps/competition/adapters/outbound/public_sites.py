"""Fixed-host reads of two public korfbal result sites; no Sportlink credentials.

Both sites publish the access key their own pages use. It is read from the site
on first use and never stored, so a rotated key keeps working.
"""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
import re
from typing import Any

import requests

from apps.competition.adapters.outbound.sportlink import retry_delay
from apps.competition.application.ports import FetchResult, RequestGate, TransportError
from apps.competition.models import HistoricalResource
from apps.competition.services.history_sites import KORFBALNL, PAGE_SIZE


USER_AGENT = "KorfConnect history import (+https://korfconnect.nl)"
KORFBALNL_SITE = "https://competitie.korfbal.nl/"
KORFBALNL_API = "https://api.korfbal.nl/v1/"
UITSLAGEN_SITE = "https://korfbal-uitslagen.nl/"
# The site lists at most this many rows; a full page means another one follows.
KORFBALNL_LIMIT = 2500
MATCH_SELECT = (
    "id,date,home_score,away_score,"
    "pool:pools(ref_id,name,division:divisions(name),"
    "phase:season_phases(sport:sports(ref_id))),"
    "home:pool_teams!home_pool_team_id(ref_id,name,club:clubs(ref_id,name)),"
    "away:pool_teams!away_pool_team_id(ref_id,name,club:clubs(ref_id,name))"
)


class SiteError(Exception):
    """A site answered with an HTTP status the caller reports unchanged."""

    def __init__(self, response: requests.Response) -> None:
        """Keep only the transport result; response bodies are never retained."""
        super().__init__("Result site HTTP failure")
        self.result = FetchResult(
            status=response.status_code,
            retry_after=retry_delay(response.headers.get("Retry-After", "60")),
        )


class PublicSiteClient:
    """Read known collections of the two result sites within the request budget."""

    def __init__(self) -> None:
        """Open one identified connection; site keys are loaded on first use."""
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        self.korfbalnl_token = ""
        self.uitslagen: tuple[str, str] | None = None

    def close(self) -> None:
        """Release pooled network connections."""
        self.session.close()

    def fetch(self, resource: HistoricalResource, gate: RequestGate) -> FetchResult:
        """Read one checkpoint; every HTTP attempt is paced and counted.

        Raises:
            ValueError: The checkpoint kind does not belong to its site.

        """
        try:
            if resource.provider == KORFBALNL and resource.kind == "catalogue":
                data = self.korfbalnl_catalogue(resource, gate)
            elif resource.provider == KORFBALNL and resource.kind == "club_matches":
                data = {
                    "weeks": self.korfbalnl(
                        f"matches/club/{resource.source_id}",
                        {
                            "sort": "date",
                            "start": resource.start_date.isoformat(),
                            "end": resource.end_date.isoformat(),
                            "limit": KORFBALNL_LIMIT,
                        },
                        gate,
                    )
                }
            elif resource.kind == "match_page":
                data = {"rows": self.uitslagen_page(resource, gate)}
            else:
                raise ValueError("Unsupported result site resource")
        except SiteError as error:
            return error.result
        return FetchResult(status=HTTPStatus.OK, data=data)

    def get(
        self,
        url: str,
        gate: RequestGate,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        """Send one paced request to a fixed host.

        Raises:
            TransportError: The connection failed.
            SiteError: The site did not answer with HTTP 200.

        """
        gate.before_request()
        try:
            response = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=(10, 60),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise TransportError("Result site connection failed") from None
        if response.status_code != HTTPStatus.OK:
            raise SiteError(response)
        return response

    def korfbalnl(self, path: str, params: dict[str, Any], gate: RequestGate) -> list:
        """Read one collection of KNKV's former competition site.

        Raises:
            TransportError: The site no longer publishes its key, or sent no list.

        """
        if not self.korfbalnl_token:
            page = self.get(
                KORFBALNL_SITE + "configs/environment.js",
                gate,
                headers={"Accept": "*/*"},
            )
            found = re.search(r"token:\s*'([A-Za-z0-9._-]+)'", page.text)
            if found is None:
                raise TransportError("Result site key not found")
            self.korfbalnl_token = found[1]
        body = self.get(
            KORFBALNL_API + path,
            gate,
            params=params,
            headers={"Authorization": f"Bearer {self.korfbalnl_token}"},
        ).json()
        if not isinstance(body, list):
            raise TransportError("Expected a result site collection")
        return body

    def korfbalnl_catalogue(
        self, resource: HistoricalResource, gate: RequestGate
    ) -> dict[str, list]:
        """Read the clubs, sports and one edition's poules with their classes."""
        edition = resource.season.start_date.year
        poules: list = []
        for season in self.korfbalnl("seasons", {"limit": 100}, gate):
            if season.get("year") != edition:
                continue
            page = 1
            while True:
                rows = self.korfbalnl(
                    "poules",
                    {
                        "season": season["_id"],
                        "populate": "division",
                        "limit": KORFBALNL_LIMIT,
                        "page": page,
                    },
                    gate,
                )
                # The series tells a full-year competition from a half-season one.
                poules.extend({**row, "serie": season.get("serie")} for row in rows)
                if len(rows) < KORFBALNL_LIMIT:
                    break
                page += 1
        return {
            "clubs": self.korfbalnl("clubs", {"limit": KORFBALNL_LIMIT}, gate),
            "sports": self.korfbalnl("sports", {}, gate),
            "poules": poules,
        }

    def uitslagen_page(self, resource: HistoricalResource, gate: RequestGate) -> list:
        """Read the edition's next matches after the checkpoint's match number.

        Raises:
            TransportError: The site no longer publishes its data address or key.

        """
        if self.uitslagen is None:
            home = self.get(UITSLAGEN_SITE, gate, headers={"Accept": "text/html"})
            script = re.search(r"/assets/index-[A-Za-z0-9_-]+\.js", home.text)
            if script is None:
                raise TransportError("Result site script not found")
            source = self.get(
                UITSLAGEN_SITE + script[0].lstrip("/"), gate, headers={"Accept": "*/*"}
            ).text
            host = re.search(r"https://[a-z0-9]+\.supabase\.co", source)
            key = re.search(
                r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", source
            )
            if host is None or key is None:
                raise TransportError("Result site key not found")
            self.uitslagen = (host[0], key[0])
        host, key = self.uitslagen
        body = self.get(
            host + "/rest/v1/matches",
            gate,
            params=[
                ("select", MATCH_SELECT),
                ("date", f"gte.{resource.start_date.isoformat()}"),
                ("date", f"lt.{(resource.end_date + timedelta(days=1)).isoformat()}"),
                ("id", f"gt.{int(resource.source_id)}"),
                ("order", "id.asc"),
                ("limit", PAGE_SIZE),
            ],
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        ).json()
        if not isinstance(body, list):
            raise TransportError("Expected a result site collection")
        return body
