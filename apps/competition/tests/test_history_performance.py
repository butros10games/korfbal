"""Guard historical request scaling with overlapping, fabricated provider graphs."""

from collections import Counter
from datetime import date
from typing import Any

import pytest

from apps.competition.application.ports import FetchResult
from apps.competition.models import (
    Club,
    HistoricalDiscovery,
    HistoricalResource,
    Match,
    ResultRevision,
)
from apps.competition.services.history import seed
from apps.competition.services.history_checkpoint import checkpoint
from apps.competition.services.history_worker import run_history
from apps.competition.services.traffic import TrafficGate
from apps.competition.tests.test_importer import match_payload, team_payload
from apps.schedule.models import Season


class OverlappingGraphClient:
    """Serve two overlapping club feeds and one authoritative bulk poule."""

    def __init__(self, provider: str, count: int) -> None:
        """Build distinct identities whose scores include valid zero results."""
        self.provider = provider
        self.count = count
        self.calls: list[tuple[str, str]] = []
        self.app_rows = [
            {
                **match_payload(),
                "PublicMatchId": f"M{index}",
                "MatchDateTime": "2025-05-10T13:30:00+0200",
                "HomeResult": {"Score": index % 20},
                "AwayResult": {"Score": (index + 3) % 20},
            }
            for index in range(1, count + 1)
        ]
        self.dataservice_rows = [
            {
                "wedstrijdcode": index,
                "wedstrijddatum": "2025-05-10T13:30:00+0200",
                "thuisteamid": 1,
                "thuisteam": "Example 1",
                "thuisteamclubrelatiecode": "C1",
                "uitteamid": 2,
                "uitteam": "Example 2",
                "uitteamclubrelatiecode": "C2",
                "uitslag": f"{index % 20} - {(index + 3) % 20}",
            }
            for index in range(1, count + 1)
        ]

    def fetch(self, resource: HistoricalResource, gate: TrafficGate) -> FetchResult:
        """Count actual HTTP resources, matching the adapter's local pool node."""
        if self.provider == "dataservice" and resource.kind == "pool":
            return FetchResult(200, {})
        gate.before_request()
        self.calls.append((resource.kind, resource.source_id))
        if self.provider == "app":
            if resource.kind == "match":
                return FetchResult(200, self.app_rows[int(resource.source_id[1:]) - 1])
            return FetchResult(
                200,
                {
                    "ResultsFiltered": False,
                    "MatchResult": self.app_rows,
                    "PoolStanding": {
                        "PoolStandingTeam": [
                            {**team_payload(identifier), "TotalMatches": self.count}
                            for identifier in ("T1", "T2")
                        ]
                    },
                },
            )
        return FetchResult(200, self.dataservice_response(resource))

    def dataservice_response(self, resource: HistoricalResource) -> dict[str, Any]:
        """Expose the same exact fixtures through club, detail and pool routes."""
        if resource.kind in {"window", "pool_window"}:
            return {
                "rows": (
                    self.dataservice_rows
                    if resource.start_date <= date(2025, 5, 10) <= resource.end_date
                    else []
                ),
                "wire_start": resource.start_date,
            }
        if resource.kind == "match":
            return {
                "wedstrijdinformatie": {
                    "wedstrijddatetime": "2025-05-10T13:30:00+0200",
                    "poulecode": 10,
                    "thuisteamid": 1,
                    "uitteamid": 2,
                    "poule": "A",
                    "klasse": "Example",
                }
            }
        return {
            "rows": [
                {
                    "teamnaam": f"Example {index}",
                    "clubrelatiecode": f"C{index}",
                    "gespeeldewedstrijden": self.count,
                }
                for index in (1, 2)
            ]
        }

    def close(self) -> None:
        """Leave the synthetic graph without open network connections."""


@pytest.mark.django_db
@pytest.mark.parametrize("count", [10, 100])
@pytest.mark.parametrize("provider", ["app", "dataservice"])
def test_overlapping_graph_request_count_does_not_scale_with_fixtures(
    provider: str, count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two discovery paths retain every score using fixed bulk-request counts."""
    monkeypatch.setattr("apps.competition.services.traffic.time.sleep", lambda _: None)
    season = Season.objects.create(
        name="history-scaling-2025",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 30),
    )
    client = OverlappingGraphClient(provider, count)
    if provider == "app":
        for index in range(1, count + 1):
            for club in ("C1", "C2"):
                seed(
                    season,
                    provider,
                    "match",
                    f"M{index}",
                    reference=f"club/{club}",
                )
        assert HistoricalDiscovery.objects.count() == count * 2
    else:
        for club in ("C1", "C2"):
            Club.objects.create(external_id=club, name=f"Club {club}")
            seed(
                season,
                provider,
                "window",
                club,
                start=date(2025, 5, 1),
                end=date(2025, 5, 31),
                sport="KORFBALL-VE-WK",
            )

    result = run_history(lambda: client, budget=1000, publish=False)

    expected_calls = (
        {"match": 1, "pool": 1}
        if provider == "app"
        else {"window": 2, "match": 1, "pool_window": 1, "standing": 1, "members": 1}
    )
    assert Counter(kind for kind, _ in client.calls) == expected_calls
    assert result["http_requests"] == sum(expected_calls.values())
    assert result["blocked"] == result["failed"] == 0
    assert not HistoricalResource.objects.exclude(state="fetched").exists()
    assert HistoricalResource.objects.get(kind="pool").coverage == "complete"
    prefix = "M" if provider == "app" else "ds:"
    assert dict(Match.objects.values_list("external_id", "home_score")) == {
        f"{prefix}{index}": index % 20 for index in range(1, count + 1)
    }
    assert set(Match.objects.values_list("external_id", "away_score")) == {
        (f"{prefix}{index}", (index + 3) % 20) for index in range(1, count + 1)
    }
    assert ResultRevision.objects.count() == count

    assert run_history(lambda: client, budget=1000, publish=False)["http_requests"] == 0
    assert len(client.calls) == sum(expected_calls.values())
    assert ResultRevision.objects.count() == count


@pytest.mark.django_db
@pytest.mark.parametrize("count", [10, 100])
def test_split_year_bulk_windows_precede_remaining_match_requests(
    count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fetch older bulk intervals before season-wide details for the same fixtures."""
    monkeypatch.setattr("apps.competition.services.traffic.time.sleep", lambda _: None)
    season = Season.objects.create(
        name="history-split-scaling-2025",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
    )
    client = OverlappingGraphClient("dataservice", count)
    for club in ("C1", "C2"):
        Club.objects.create(external_id=club, name=f"Club {club}")
    initial = seed(season, "dataservice", "window", "C1", sport="KORFBALL-VE-WK")
    # Resume an imported result snapshot whose detail discoveries cover the season.
    checkpoint(
        initial,
        {"rows": client.dataservice_rows, "wire_start": initial.start_date},
    )

    result = run_history(lambda: client, budget=1000, publish=False)

    expected_calls = {"match": 1, "pool_window": 2, "standing": 1, "members": 1}
    assert Counter(kind for kind, _ in client.calls) == expected_calls
    assert result["http_requests"] == sum(expected_calls.values())
    assert result["blocked"] == result["failed"] == 0
    assert not HistoricalResource.objects.exclude(
        state__in=("fetched", "split")
    ).exists()
    assert HistoricalResource.objects.get(kind="pool").coverage == "complete"
    assert set(
        Match.objects.values_list("external_id", "home_score", "away_score")
    ) == {
        (f"ds:{index}", index % 20, (index + 3) % 20) for index in range(1, count + 1)
    }
    assert ResultRevision.objects.count() == count
    assert run_history(lambda: client, budget=1000, publish=False)["http_requests"] == 0
