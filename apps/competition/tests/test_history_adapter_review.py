"""Historical wire contracts use endpoint-specific documented provider limits."""

from datetime import date, timedelta
from unittest.mock import Mock

import pytest
import requests

from apps.competition.adapters.outbound.history import HistoryClient, window_parameters
from apps.competition.application.ports import TransportError
from apps.competition.models import HistoricalResource
from apps.competition.services.history import HistoryUnavailableError


CLUB_RESULT_LIMIT = 500


@pytest.mark.parametrize("kind", ["window", "pool_window"])
def test_window_parameters_only_request_supported_row_limit(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Club responses cap at 500; poule requests omit unsupported aantalregels."""
    today = date(2026, 9, 8)
    monkeypatch.setattr(
        "apps.competition.adapters.outbound.history.timezone.localdate", lambda: today
    )
    resource = HistoricalResource(
        provider="dataservice",
        kind=kind,
        source_id="1",
        start_date=today - timedelta(days=15),
        end_date=today - timedelta(days=1),
    )
    parameters, wire_start = window_parameters(resource)
    assert wire_start <= resource.start_date
    assert resource.start_date - wire_start < timedelta(weeks=1)
    if kind == "window":
        assert parameters["aantalregels"] == CLUB_RESULT_LIMIT
    else:
        assert "aantalregels" not in parameters


def test_inaccessible_club_window_spends_no_wire_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-retention club reads fail before counting or sending an HTTP call."""
    monkeypatch.setattr(
        "apps.competition.adapters.outbound.history.timezone.localdate",
        lambda: date(2026, 9, 8),
    )
    resource = HistoricalResource(
        provider="dataservice",
        kind="window",
        source_id="1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
    )
    client = HistoryClient(dataservice_id="synthetic")
    client.session.get = Mock()
    gate = Mock()
    try:
        with pytest.raises(HistoryUnavailableError, match="52_week_limit"):
            client.fetch(resource, gate)
        gate.before_request.assert_not_called()
        client.session.get.assert_not_called()
    finally:
        client.close()


def test_dataservice_transport_failure_keeps_credentials_out_of_error() -> None:
    """Provider URLs can contain client IDs and must never enter stored errors."""
    client = HistoryClient(dataservice_id="synthetic")
    client.session.get = Mock(
        side_effect=requests.ConnectionError("https://provider/?client_id=synthetic")
    )
    gate = Mock()
    try:
        with pytest.raises(
            TransportError, match=r"^Historical provider connection failed$"
        ):
            client.fetch(
                HistoricalResource(provider="dataservice", kind="match", source_id="1"),
                gate,
            )
        gate.before_request.assert_called_once()
    finally:
        client.close()
