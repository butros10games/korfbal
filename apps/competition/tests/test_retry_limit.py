"""Exhausted feeds stop consuming requests and remain available for review."""

from collections import defaultdict
from datetime import timedelta
from unittest.mock import Mock

from django.utils import timezone
import pytest

from apps.competition.application.ports import FetchResult
from apps.competition.models import SyncResource
from apps.competition.services import sync as sync_service
from apps.competition.services.polling import PollJob, PollPlanner
from apps.competition.services.resources import MAX_FEED_FAILURES
from apps.schedule.models import Season


@pytest.mark.django_db
def test_five_retries_then_no_more_requests(season: Season) -> None:
    """Six total failures survive a reload and cannot be fetched again automatically."""
    resource = SyncResource.objects.create(
        season=season, kind="clubs", next_sync_at=timezone.now()
    )
    client = Mock()
    client.fetch.return_value = FetchResult(500)
    summary = defaultdict(int)
    for _ in range(MAX_FEED_FAILURES):
        sync_service._fetch_one(PollJob(resource, set(), 2), client, summary, Mock())
    resource.refresh_from_db()
    assert resource.failures == MAX_FEED_FAILURES
    sync_service._fetch_one(PollJob(resource, set(), 2), client, summary, Mock())
    assert client.fetch.call_count == MAX_FEED_FAILURES
    assert PollPlanner(season, timezone.now() + timedelta(days=30)).next_job() is None
    assert resource.last_error == "http_500"


@pytest.mark.django_db
def test_final_allowed_retry_can_recover(season: Season) -> None:
    """Success on retry five clears the consecutive failure count."""
    resource = SyncResource.objects.create(
        season=season,
        kind="clubs",
        next_sync_at=timezone.now(),
        failures=MAX_FEED_FAILURES - 1,
        last_error="http_500",
    )
    client = Mock()
    client.fetch.return_value = FetchResult(200, {"Club": []})
    sync_service._fetch_one(
        PollJob(resource, set(), 2), client, defaultdict(int), Mock()
    )
    resource.refresh_from_db()
    assert not resource.failures
    assert not resource.last_error
    assert resource.fetched_at is not None
