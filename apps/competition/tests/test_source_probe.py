"""Synthetic source probes preserve content and count every wire attempt."""

from datetime import date
from io import StringIO
import json
from typing import Never
from unittest.mock import Mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
import pytest
from pytest_django.fixtures import Settings

from apps.competition.application.ports import FetchResult, RequestGate
from apps.competition.management.commands.probe_competition_sources import (
    probe_selection,
)
from apps.competition.models import HistoricalResource, Match, SyncLease, TrafficState
from apps.competition.queries.source_coverage import source_coverage_preview
from apps.competition.services.source_probe import probe_sources, summarize_response
from apps.competition.services.traffic import TrafficGate
from apps.competition.tests.test_importer import match_payload
from apps.schedule.domain.competition_context import INDOOR_PHASE
from apps.schedule.models import Season
from apps.schedule.tests.season_builders import playing_season


INITIAL_ATTEMPTS = 3
AUTHENTICATED_REQUESTS = 2
MALFORMED_BATCH_RESOURCES = 2
RETURNED_FIXTURES = 3
UNIQUE_FIXTURES = 2


@pytest.fixture
def probe_resource() -> HistoricalResource:
    """Use one saved observation in the known empty 2023 edition."""
    anchor = playing_season(INDOOR_PHASE, 2023)
    return HistoricalResource.objects.create(
        season=anchor,
        provider="app",
        kind="edition_pool",
        source_id="10",
        key="synthetic-source-probe",
        start_date=date(2023, 7, 1),
        end_date=date(2024, 6, 30),
        state="fetched",
        coverage="empty",
        reason="no_season_data",
        attempts=INITIAL_ATTEMPTS,
        evidence={"observed": "synthetic"},
        etag="old-validator",
    )


def _selection(resource: HistoricalResource) -> list[HistoricalResource]:
    return probe_selection({
        "edition": 2023,
        "resource": [resource.pk],
        "provider": None,
        "kind": None,
        "source_id": None,
        "limit": 20,
    })


@pytest.mark.django_db
def test_default_preview_makes_no_provider_calls_or_database_writes(
    probe_resource: HistoricalResource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an empty edition with a scan marker stays a read-only observation."""
    HistoricalResource.objects.create(
        season=probe_resource.season,
        provider="app",
        kind="edition_scan",
        source_id="2023",
        key="synthetic-scan",
        start_date=probe_resource.start_date,
        end_date=probe_resource.end_date,
        state="fetched",
        coverage="partial",
        evidence={"low": None, "high": None},
    )
    factory = Mock(side_effect=AssertionError("Preview opened a provider"))
    monkeypatch.setattr(
        "apps.competition.management.commands.probe_competition_sources.source_probe_client",
        factory,
    )
    before = list(HistoricalResource.objects.order_by("pk").values())
    output = StringIO()
    with CaptureQueriesContext(connection) as captured:
        call_command("probe_competition_sources", edition=2023, stdout=output)
    payload = json.loads(output.getvalue())
    assert payload["mode"] == "preview"
    assert payload["http_requests"] == payload["domain_writes"] == 0
    assert payload["operational_writes"] == []
    assert payload["coverage_certified_complete"] is False
    assert payload["scan_ranges"] == [{"evidence__low": None, "evidence__high": None}]
    assert payload["app_pool_checkpoints"] == 1
    assert list(HistoricalResource.objects.order_by("pk").values()) == before
    assert not SyncLease.objects.exists()
    assert not TrafficState.objects.exists()
    assert not any(
        query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for query in captured
    )
    factory.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "options", [{"probe": True}, {"probe": True, "max_requests": 1}]
)
def test_network_probe_requires_both_explicit_budget_and_selection(
    probe_resource: HistoricalResource,
    options: dict,
) -> None:
    """Selecting an edition never implicitly drains its existing queue."""
    with pytest.raises(CommandError):
        call_command("probe_competition_sources", edition=2023, **options)
    assert not SyncLease.objects.exists()
    assert (
        HistoricalResource.objects.get(pk=probe_resource.pk).attempts
        == INITIAL_ATTEMPTS
    )


@pytest.mark.django_db
def test_budgeted_network_probe_writes_only_operational_limits(
    probe_resource: HistoricalResource,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    """A real gate accounts for authentication as well as the public response."""
    settings.SPORTLINK_HOURLY_LIMIT = 100
    settings.SPORTLINK_DAILY_LIMIT = 1000
    monkeypatch.setattr(
        "apps.competition.services.source_probe.TrafficGate",
        lambda budget, owner, **kwargs: TrafficGate(budget, owner, spacing=0, **kwargs),
    )
    selected = _selection(probe_resource)
    assert selected[0]._state.adding
    assert not selected[0].etag
    before = HistoricalResource.objects.get(pk=probe_resource.pk).__dict__.copy()
    client = Mock()

    def fetch(resource: HistoricalResource, gate: RequestGate) -> FetchResult:
        gate.before_request()  # Synthetic authentication wire request.
        gate.before_request()  # Synthetic pool response.
        return FetchResult(
            200,
            {
                "MatchResult": [],
                "PoolStanding": {"PoolStandingTeam": [{"TotalMatches": 0}]},
                "PrivatePerson": "SYNTHETIC-PERSON-SHOULD-NOT-ESCAPE",
            },
        )

    client.fetch.side_effect = fetch
    result = probe_sources(selected, lambda: client, budget=AUTHENTICATED_REQUESTS)
    assert result["http_requests"] == AUTHENTICATED_REQUESTS
    assert result["availability"] == {"available": 1}
    assert result["standings_only"] == 1
    assert (
        result["domain_writes"]
        == result["checkpoint_writes"]
        == result["publication_writes"]
        == 0
    )
    assert "provider_traffic" in result["operational_writes"]
    assert "SYNTHETIC-PERSON" not in json.dumps(result)
    assert (
        TrafficState.objects.get(key="sportlink").hour_requests
        == AUTHENTICATED_REQUESTS
    )
    fresh = HistoricalResource.objects.get(pk=probe_resource.pk)
    assert {key: value for key, value in fresh.__dict__.items() if key != "_state"} == {
        key: value for key, value in before.items() if key != "_state"
    }
    assert Season.objects.get(pk=probe_resource.season_id).data_coverage == "unknown"
    assert not Match.objects.exists()
    client.close.assert_called_once()


@pytest.mark.django_db
def test_authentication_cannot_exceed_explicit_http_budget(
    probe_resource: HistoricalResource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh consumes the sole request and defers the subsequent endpoint."""
    monkeypatch.setattr(
        "apps.competition.services.source_probe.TrafficGate",
        lambda budget, owner, **kwargs: TrafficGate(budget, owner, spacing=0, **kwargs),
    )
    client = Mock()

    def fetch(resource: HistoricalResource, gate: RequestGate) -> Never:
        gate.before_request()
        gate.before_request()
        raise AssertionError("Request budget did not stop HTTP")

    client.fetch.side_effect = fetch
    result = probe_sources(_selection(probe_resource), lambda: client, budget=1)
    assert result["status"] == "budget_deferred"
    assert result["http_requests"] == 1
    assert result["attempted_resources"] == 0
    assert HistoricalResource.objects.get(pk=probe_resource.pk).coverage == "empty"


@pytest.mark.django_db
def test_probe_reports_months_sports_and_routing_without_certifying_completeness(
    probe_resource: HistoricalResource,
) -> None:
    """Returned source rows, duplicate copies and out-of-edition dates differ."""
    row = match_payload()
    row["MatchDateTime"] = "2024-03-07T14:00:00+01:00"
    outside = {
        **row,
        "PublicMatchId": "M2",
        "MatchDateTime": "2025-05-01T14:00:00+02:00",
    }
    summary = summarize_response(probe_resource, {"MatchResult": [row, row, outside]})
    assert summary["returned_rows"] == RETURNED_FIXTURES
    assert summary["unique_fixtures"] == UNIQUE_FIXTURES
    assert summary["months"] == {"2024-03": 1, "2025-05": 1}
    assert summary["sports"] == {"outdoor": 2}
    assert summary["routing_candidates"] == {"spring": 1}
    assert summary["skipped"] == {"outside_season_dates": 1}
    assert source_coverage_preview(2023)["coverage_certified_complete"] is False
    assert Season.objects.get(pk=probe_resource.season_id).data_coverage == "unknown"


@pytest.mark.django_db
def test_probe_distinguishes_empty_result_from_rows_all_skipped(
    probe_resource: HistoricalResource,
) -> None:
    """A public site response rejected by normal source rules is not empty."""
    site = HistoricalResource(
        season=probe_resource.season,
        provider="uitslagen",
        kind="match_page",
        source_id="0",
        start_date=probe_resource.start_date,
        end_date=probe_resource.end_date,
    )
    assert summarize_response(site, {"rows": []})["availability"] == "empty"
    skipped = summarize_response(
        site, {"rows": [{"id": 1, "home_score": None, "away_score": None}]}
    )
    assert skipped["availability"] == "all_rows_skipped"
    assert skipped["skipped"] == {"not_played": 1}


@pytest.mark.django_db
def test_malformed_app_string_rows_never_escape_probe_output(
    probe_resource: HistoricalResource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-supplied strings are invalid fixtures, never diagnostic codes."""
    private_text = "SYNTHETIC-PRIVATE-PROVIDER-ROW"
    with pytest.raises(TypeError, match="fixture objects"):
        summarize_response(probe_resource, {"MatchResult": [private_text]})
    monkeypatch.setattr(
        "apps.competition.services.source_probe.TrafficGate",
        lambda budget, owner, **kwargs: TrafficGate(budget, owner, spacing=0, **kwargs),
    )
    client = Mock()

    def fetch(resource: HistoricalResource, gate: RequestGate) -> FetchResult:
        gate.before_request()
        return FetchResult(200, {"MatchResult": [private_text]})

    client.fetch.side_effect = fetch
    result = probe_sources(_selection(probe_resource), lambda: client, budget=1)
    assert result["availability"] == {"invalid": 1}
    assert result["skipped"] == {}
    assert private_text not in json.dumps(result)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "data"),
    [
        (
            "edition_pool",
            {"MatchResult": [], "PoolStanding": "SYNTHETIC-PRIVATE-ENVELOPE"},
        ),
        ("edition_pool", {"MatchResult": [], "PoolStanding": []}),
        (
            "edition_pool",
            {
                "MatchResult": [],
                "PoolStanding": {"PoolStandingTeam": ["SYNTHETIC-PRIVATE-ROW"]},
            },
        ),
        ("edition_pool", {"Error": True, "MatchResult": []}),
        (
            "edition_pool",
            {"Error": {"description": "SYNTHETIC-PRIVATE-ERROR"}, "MatchResult": []},
        ),
        (
            "edition_team",
            {"UnboundMatchResults": "SYNTHETIC-PRIVATE-ENVELOPE", "Pool": []},
        ),
        ("edition_team", {"UnboundMatchResults": [], "Pool": []}),
        (
            "edition_team",
            {"UnboundMatchResults": {"MatchResult": ["SYNTHETIC-PRIVATE-ROW"]}},
        ),
        ("edition_team", {"Pool": ["SYNTHETIC-PRIVATE-POOL"]}),
        ("edition_team", []),
        ("edition_team", None),
    ],
)
def test_malformed_envelopes_are_invalid_and_do_not_stop_a_bounded_probe(
    probe_resource: HistoricalResource,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    data: object,
) -> None:
    """Application errors and malformed rows never claim empty availability."""
    selected = _selection(probe_resource)
    selected[0].kind = kind
    selected[0].source_id = "T1" if kind == "edition_team" else "10"
    valid = _selection(probe_resource)[0]
    valid.source_id = "11"
    selected.append(valid)
    monkeypatch.setattr(
        "apps.competition.services.source_probe.TrafficGate",
        lambda budget, owner, **kwargs: TrafficGate(budget, owner, spacing=0, **kwargs),
    )
    replies = iter([Mock(status=200, data=data), FetchResult(200, {"MatchResult": []})])
    client = Mock()

    def fetch(resource: HistoricalResource, gate: RequestGate) -> FetchResult | Mock:
        gate.before_request()
        return next(replies)

    client.fetch.side_effect = fetch
    result = probe_sources(selected, lambda: client, budget=MALFORMED_BATCH_RESOURCES)
    assert result["availability"] == {"invalid": 1, "empty": 1}
    assert (
        result["attempted_resources"]
        == result["http_requests"]
        == MALFORMED_BATCH_RESOURCES
    )
    assert result["status"] == "completed"
    assert result["skipped"] == {}
    assert "SYNTHETIC-PRIVATE" not in json.dumps(result)
    assert (
        HistoricalResource.objects.get(pk=probe_resource.pk).attempts
        == INITIAL_ATTEMPTS
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field", ["HomeTeam", "AwayTeam", "HomeResult", "AwayResult", "Pool"]
)
def test_malformed_nested_fixture_objects_are_invalid(
    probe_resource: HistoricalResource,
    field: str,
) -> None:
    """The pure fixture validator never receives a non-object nested envelope."""
    row = match_payload()
    row[field] = "SYNTHETIC-PRIVATE-NESTED-OBJECT"
    with pytest.raises(TypeError, match="public source object"):
        summarize_response(probe_resource, {"MatchResult": [row]})


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data",
    [
        {"rows": {}},
        {"rows": ["SYNTHETIC-PRIVATE-SITE-ROW"]},
        {"rows": [{"pool": "SYNTHETIC-PRIVATE-SITE-POOL"}]},
        {"rows": [{"home": {"club": "SYNTHETIC-PRIVATE-SITE-CLUB"}}]},
    ],
)
def test_malformed_public_site_collections_are_invalid(
    probe_resource: HistoricalResource,
    data: dict,
) -> None:
    """Public-site normalization rejects malformed nested rows before access."""
    site = HistoricalResource(
        season=probe_resource.season,
        provider="uitslagen",
        kind="match_page",
        source_id="0",
        start_date=probe_resource.start_date,
        end_date=probe_resource.end_date,
    )
    with pytest.raises(TypeError, match=r"Expected public|Expected a public"):
        summarize_response(site, data)
