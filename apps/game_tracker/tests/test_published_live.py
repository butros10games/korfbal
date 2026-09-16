"""Shared snapshots preserve transactional publication and public HTTP recovery."""

from http import HTTPStatus
from unittest.mock import patch

from django.db import connection, transaction
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
import pytest

from apps.game_tracker.application.ports import PublicLiveStoreError
from apps.game_tracker.composition import (
    publish_public_live_snapshot,
    published_live_store,
    read_public_live,
    record_match_change,
)
from apps.game_tracker.models import MatchLiveChange
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource
from apps.game_tracker.services.live_updates import summarize_match_changes
from apps.game_tracker.services.public_live import build_published_live
from apps.kwt_common.models import BackgroundJob
from apps.schedule.tests.match_api_test_support import create_match_graph


pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_publication_intent_rolls_back_with_revision() -> None:
    """Aborted writes neither enqueue durable work nor fence committed snapshots."""
    graph = create_match_graph(prefix="Publication rollback")
    before = read_public_live(match_id=graph.match.pk)
    with patch("apps.kwt_common.services.jobs.publish_job") as dispatch:
        with transaction.atomic():
            record_match_change(graph.match_data)
            assert BackgroundJob.objects.filter(
                task="apps.game_tracker.tasks.publish_public_live_snapshot"
            ).exists()
            transaction.set_rollback(True)
        dispatch.assert_not_called()
    graph.match_data.refresh_from_db()
    assert graph.match_data.live_revision == before["live_revision"]
    assert read_public_live(match_id=graph.match.pk) == before
    assert not BackgroundJob.objects.filter(
        task="apps.game_tracker.tasks.publish_public_live_snapshot"
    ).exists()


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_committed_job_warms_http_and_poll_without_sql(client: Client) -> None:
    """A worker prepares the shared response before spectators arrive."""
    graph = create_match_graph(prefix="Published HTTP")
    with patch("apps.kwt_common.services.jobs.publish_job"):
        record_match_change(graph.match_data, resources={LiveResource.LIVE})
    job = BackgroundJob.objects.get(
        task="apps.game_tracker.tasks.publish_public_live_snapshot"
    )
    assert job.args == [str(graph.match.pk)]
    publish_public_live_snapshot(match_id=str(graph.match.pk))
    route = f"/api/matches/{graph.match.pk}/live/"
    with CaptureQueriesContext(connection) as queries:
        response = client.get(route)
        poll = client.get(
            f"{route}poll/",
            {"since_revision": graph.match_data.live_revision, "timeout": "25"},
        )
        changed = client.get(f"{route}poll/", {"since_revision": 0})
    assert response.status_code == HTTPStatus.OK
    assert poll.json()["changed"] is False
    assert changed.json()["resources"] == ["live"]
    assert len(queries) == 0
    assert "player_groups" not in response.json()


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_client_ahead_of_cached_revision_recovers_authoritative_state() -> None:
    """A delayed publication cannot strand a viewer at an older revision."""
    graph = create_match_graph(prefix="Ahead viewer")
    old = build_published_live(str(graph.match.pk))
    with patch("apps.kwt_common.services.jobs.publish_job"):
        record_match_change(graph.match_data)
    with patch.object(published_live_store, "get", return_value=old):
        current = read_public_live(
            match_id=graph.match.pk, since_revision=graph.match_data.live_revision
        )
    assert current["live_revision"] == graph.match_data.live_revision
    assert current["changed"] is False


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_missing_history_requests_all_resources() -> None:
    """Reconnects outside retained history refresh every public resource safely."""
    graph = create_match_graph(prefix="History recovery")
    with patch("apps.kwt_common.services.jobs.publish_job"):
        record_match_change(graph.match_data, resources={LiveResource.SHOTS})
    MatchLiveChange.objects.filter(match_data=graph.match_data).delete()
    publish_public_live_snapshot(match_id=str(graph.match.pk))
    payload = read_public_live(match_id=graph.match.pk, since_revision=0)
    assert payload["resources"] == sorted(
        resource.value for resource in ALL_LIVE_RESOURCES
    )


def test_storage_failure_recovers_reads_but_retries_publication() -> None:
    """Cache outages preserve reads and keep failed publications retryable."""
    graph = create_match_graph(prefix="Unavailable publication")
    with (
        patch.object(published_live_store, "get", side_effect=PublicLiveStoreError),
        patch.object(published_live_store, "put", side_effect=PublicLiveStoreError),
    ):
        payload = read_public_live(match_id=graph.match.pk)
        assert payload["match_id"] == str(graph.match.pk)
        with pytest.raises(PublicLiveStoreError):
            publish_public_live_snapshot(match_id=str(graph.match.pk))


def test_warm_snapshot_does_not_bypass_filtered_route_lookup(client: Client) -> None:
    """Public caching retains explicit catalogue filters and missing-match responses."""
    graph = create_match_graph(prefix="Filtered public snapshot")
    other = create_match_graph(prefix="Other public snapshot")
    read_public_live(match_id=graph.match.pk)
    route = f"/api/matches/{graph.match.pk}/live/"
    response = client.get(route, {"team": str(other.home_team.pk)})
    assert response.status_code == HTTPStatus.NOT_FOUND
    graph.match.delete()
    assert client.get(route).status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    "update_field", ["home_team", "away_team", "home_team_id", "away_team_id"]
)
def test_saved_team_change_advances_public_revision(update_field: str) -> None:
    """Changing a match's team identity invalidates its previous public snapshot."""
    graph = create_match_graph(prefix="Changed team snapshot")
    other = create_match_graph(prefix="Replacement team snapshot")
    before = read_public_live(match_id=graph.match.pk)
    setattr(graph.match, update_field.removesuffix("_id"), other.home_team)
    graph.match.save(update_fields=[update_field])
    after = read_public_live(match_id=graph.match.pk)
    assert after["live_revision"] > before["live_revision"]


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_lost_commit_notification_recovers_when_snapshot_expires() -> None:
    """A missing fence and delayed worker cannot leave public state stale forever."""
    graph = create_match_graph(prefix="Missed publication")
    before = read_public_live(match_id=graph.match.pk)
    cached = published_live_store.get(str(graph.match.pk))
    assert cached is not None
    with (
        patch.object(published_live_store, "invalidate"),
        patch("apps.game_tracker.composition.change_publisher.prepare_snapshot"),
        patch("apps.kwt_common.services.jobs.publish_job"),
    ):
        graph.match_data.status = "finished"
        graph.match_data.save(update_fields=["status"])
    assert read_public_live(match_id=graph.match.pk) == before
    with patch(
        "apps.game_tracker.adapters.outbound.published_live_store.time",
        return_value=cached["created_at"] + 31,
    ):
        after = read_public_live(match_id=graph.match.pk)
    assert after["status"] == "finished"
    assert after["live_revision"] > before["live_revision"]


@pytest.mark.parametrize("since_revision", [-1, 0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize("missing_history", [False, True])
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_compact_resource_history_matches_authoritative_summary(
    since_revision: int, missing_history: bool
) -> None:
    """Compact shared metadata preserves every reconnect resource decision."""
    graph = create_match_graph(prefix="Compact resource history")
    with patch("apps.kwt_common.services.jobs.publish_job"):
        for resource in (
            LiveResource.LIVE,
            LiveResource.SHOTS,
            LiveResource.LIVE,
            LiveResource.STATS,
        ):
            record_match_change(graph.match_data, resources={resource})
    if missing_history:
        MatchLiveChange.objects.filter(match_data=graph.match_data, revision=2).delete()
    expected = summarize_match_changes(graph.match_data, since_revision=since_revision)
    publish_public_live_snapshot(match_id=str(graph.match.pk))
    actual = read_public_live(match_id=graph.match.pk, since_revision=since_revision)
    assert actual.get("resources", []) == sorted(r.value for r in expected.resources)
