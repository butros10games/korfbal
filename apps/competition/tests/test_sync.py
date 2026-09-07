"""Verify checkpoints, global pacing and failure recovery without real HTTP."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import Mock, patch

from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.application.ports import FetchResult
from apps.competition.models import Club, SyncLease, SyncResource
from apps.competition.services.importer import enqueue
from apps.competition.services.sync import sync
from apps.schedule.models import Season


@pytest.fixture
def season() -> Season:
    """Provide a synthetic active season."""
    return Season.objects.create(
        name="2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )


@pytest.mark.django_db
def test_resume_etag_and_not_modified(season: Season) -> None:
    """Successful resources are skipped until due and retain conditional headers."""
    client = Mock(spec=SportlinkClient)
    client.fetch.return_value = FetchResult(200, {"Club": []}, '"v1"')
    with patch("apps.competition.services.traffic.time.sleep"):
        first = sync(season, client, budget=1)
        second = sync(season, client, budget=1)
    assert first["updated"] == 1
    assert second["requests"] == 0
    resource = SyncResource.objects.get()
    assert resource.etag == '"v1"'
    resource.next_sync_at = timezone.now() - timedelta(seconds=1)
    resource.save()
    client.fetch.return_value = FetchResult(304)
    with patch("apps.competition.services.traffic.time.sleep"):
        third = sync(season, client, budget=1)
    assert third["unchanged"] == 1
    resource.refresh_from_db()
    assert resource.etag == '"v1"'


@pytest.mark.django_db
@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_and_rate_limit_stop_entire_run(season: Season, status: int) -> None:
    """Do not hammer other queued resources when a session/rate limit fails."""
    client = Mock(spec=SportlinkClient)
    client.fetch.return_value = FetchResult(status, retry_after=120)
    result = sync(season, client, budget=10)
    assert result["requests"] == 1
    assert result["failed"] == 1
    assert SyncLease.objects.get().expires_at > timezone.now()
    assert SyncResource.objects.get().last_error == f"http_{status}"


@pytest.mark.django_db
def test_malformed_payload_does_not_checkpoint(season: Season) -> None:
    """Retry schema failures without marking a partially imported directory fresh."""
    client = Mock(spec=SportlinkClient)
    client.fetch.return_value = FetchResult(
        200, {"Club": [{"ClubId": "1", "ClubName": "Test"}, {}]}
    )
    with patch("apps.competition.services.traffic.time.sleep"):
        result = sync(season, client, budget=1)
    assert result["failed"] == 1
    assert SyncResource.objects.get().fetched_at is None
    assert not Club.objects.exists()


@pytest.mark.django_db
def test_concurrent_import_is_rejected(season: Season) -> None:
    """Only one provider-wide importer can run at a time."""
    SyncLease.objects.create(
        key="sportlink", expires_at=timezone.now() + timedelta(minutes=5)
    )
    with pytest.raises(ValueError, match="Another import"):
        sync(season, Mock(spec=SportlinkClient))


@pytest.mark.django_db
def test_transport_uses_only_verified_get_and_etag(season: Season) -> None:
    """No redirects or mutation endpoints can receive the stored bearer token."""
    enqueue(season, "clubs")
    resource = SyncResource.objects.get()
    resource.etag = '"known"'
    client = SportlinkClient("synthetic-test-token", user_agent="synthetic-client")
    response = Mock(status_code=304, headers={})
    with patch.object(client.session, "get", return_value=response) as get:
        assert client.fetch(resource).status == response.status_code
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["headers"] == {
        "If-None-Match": '"known"',
        "X-Navajo-Version": "1",
    }
    assert client.session.headers["X-Navajo-Instance"] == "KNKV"
    assert get.call_args.args[0].endswith("/club/Clubs")
    client.close()
