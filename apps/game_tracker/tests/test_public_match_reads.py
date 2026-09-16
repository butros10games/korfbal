"""Shared spectator responses preserve public contracts and revision recovery."""

from http import HTTPStatus
from unittest.mock import Mock, patch

from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
import pytest

from apps.game_tracker.composition import (
    prepare_public_match_reads,
    published_match_store,
    read_public_match,
)
from apps.game_tracker.models import MatchLiveChange, Shot
from apps.game_tracker.services.public_match_reads import (
    PUBLIC_MATCH_RESOURCES,
    build_public_match_reads,
    read_public_match_updates,
    render_public_match_read,
    resource_key,
)
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_player
from apps.schedule.tests.match_api_test_support import (
    create_match_graph,
    create_match_part,
)


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("resource", PUBLIC_MATCH_RESOURCES)
def test_shared_resource_matches_authoritative_route_without_sql(
    client: Client, resource: str
) -> None:
    """Warm reads skip ORM dispatch and retain the existing public response."""
    graph = create_match_graph(prefix="Shared match")
    match_id = str(graph.match.pk)
    prepare_public_match_reads(match_id=match_id)
    route = f"/api/matches/{match_id}/{resource}/"
    with CaptureQueriesContext(connection) as queries:
        actual = client.get(route)
    assert not queries
    assert actual["X-Korfbal-Public-Read"] == "shared"
    with (
        patch(
            "apps.schedule.api.public_live_cache.read_public_match", return_value=None
        ),
        patch(
            "apps.schedule.api.match_viewset_stats.read_public_match", return_value=None
        ),
        patch(
            "apps.schedule.api.match_viewset_event_reads.read_public_match",
            return_value=None,
        ),
    ):
        expected = client.get(route)
    assert actual.status_code == expected.status_code == HTTPStatus.OK
    assert actual.json() == expected.json()
    for header in (
        "Content-Type",
        "Allow",
        "X-Content-Type-Options",
        "X-Frame-Options",
    ):
        assert actual.get(header) == expected.get(header)
    assert set(actual.get("Vary", "").split(", ")) == set(
        expected.get("Vary", "").split(", ")
    )


@pytest.mark.parametrize(
    ("query", "headers"),
    [
        ("?search=missing", {}),
        ("?since_revision=-1", {}),
        ("?since_revision=bad", {}),
        ("", {"HTTP_AUTHORIZATION": "Bearer invalid"}),
        ("", {"HTTP_COOKIE": "sessionid=invalid"}),
        ("", {"HTTP_ACCEPT": "text/html"}),
    ],
)
def test_filtered_or_private_requests_keep_route_checks(
    client: Client, query: str, headers: dict[str, str]
) -> None:
    """Sharing does not bypass authentication, filtering or validation."""
    graph = create_match_graph(prefix="Shared route checks")
    with patch("apps.schedule.api.public_live_cache.read_public_match") as read:
        client.get(f"/api/matches/{graph.match.pk}/events/{query}", **headers)
    read.assert_not_called()


def test_rolled_back_state_never_enters_shared_reads() -> None:
    """Caller transactions use normal endpoints and cannot populate shared state."""
    graph = create_match_graph(prefix="Shared rollback")
    with transaction.atomic():
        with patch.object(published_match_store, "get") as get:
            assert (
                read_public_match(match_id=str(graph.match.pk), resource="summary")
                is None
            )
        get.assert_not_called()


def test_deletion_fences_delayed_publication(client: Client) -> None:
    """A previously built envelope cannot resurrect a deleted match."""
    graph = create_match_graph(prefix="Shared deletion")
    match_id = str(graph.match.pk)
    old = build_public_match_reads(match_id)
    graph.match_data.delete()
    for resource, envelope in old.items():
        key = resource_key(match_id, resource)
        published_match_store.put(key, envelope)
        assert published_match_store.get(key) is None
    response = client.get(f"/api/matches/{match_id}/summary/")
    assert response.status_code == HTTPStatus.OK
    assert response.content == b""


@pytest.mark.parametrize(
    ("since", "identity", "mode"),
    [(3, True, "delta"), (2, True, "full"), (5, True, "full"), (3, False, "full")],
)
def test_shared_delta_preserves_upserts_deletions_and_order(
    since: int, identity: bool, mode: str
) -> None:
    """Invalid cursors recover fully; valid cursors retain complete deltas."""
    envelope = {
        "revision": 4,
        "history_base": 1,
        "incomplete_revision": 3,
        "id_revisions": {"retained": 2, "new": 4, "removed": 4},
        "payload": {
            "mode": "full",
            "live_revision": 4,
            "events": [{"event_id": "retained"}, {"event_id": "new"}],
        },
    }
    result = render_public_match_read(
        envelope, resource="events", since_revision=since, current_identity=identity
    )
    assert result["mode"] == mode
    if mode == "delta":
        assert result["upsert"] == [{"event_id": "new"}]
        assert result["deleted_ids"] == ["removed"]
        assert result["order"] == ["retained", "new"]
    assert envelope["payload"]["mode"] == "full"


def test_active_timeline_recovery_matches_authoritative_reads(client: Client) -> None:
    """A removal cannot be resurrected by delayed shared publication or delta reads."""
    graph = create_match_graph(prefix="Shared active")
    graph.match_data.status = "active"
    graph.match_data.save(update_fields=["status"])
    part = create_match_part(graph)
    shot = Shot.objects.create(
        match_data=graph.match_data,
        match_part=part,
        team=graph.home_team,
        player=create_tracker_player(username="shared-shot-player"),
        scored=True,
        time=part.start_time,
    )
    match_id = str(graph.match.pk)
    old = build_public_match_reads(match_id)
    assert old["summary"]["payload"]["score"]["home"] == 1
    revision = old["events"]["revision"]
    shot.delete()
    prepare_public_match_reads(match_id=match_id)
    graph.match_data.refresh_from_db()
    pushed = read_public_match_updates(
        match_id=match_id,
        revision=graph.match_data.live_revision,
        resources=list(PUBLIC_MATCH_RESOURCES),
        store=published_match_store,
    )
    assert pushed["summary"]["score"]["home"] == 0
    for resource in ("events", "shots"):
        update = pushed[resource]
        if update.get("mode") == "delta":
            assert {
                item["event_id"] for item in old[resource]["payload"][resource]
            } <= set(update["deleted_ids"])
            assert update["order"] == []
        else:
            assert update[resource] == []
    for resource in PUBLIC_MATCH_RESOURCES:
        published_match_store.put(resource_key(match_id, resource), old[resource])
        route = f"/api/matches/{match_id}/{resource}/"
        query = (
            {"since_revision": revision, "identity_version": 3}
            if resource in {"events", "shots"}
            else {}
        )
        with CaptureQueriesContext(connection) as queries:
            actual = client.get(route, query)
        assert not queries
        with (
            patch(
                "apps.schedule.api.public_live_cache.read_public_match",
                return_value=None,
            ),
            patch(
                "apps.schedule.api.match_viewset_stats.read_public_match",
                return_value=None,
            ),
            patch(
                "apps.schedule.api.match_viewset_event_reads.read_public_match",
                return_value=None,
            ),
        ):
            expected = client.get(route, query)
        assert actual.json() == expected.json()
        if resource == "summary":
            assert actual.json()["score"]["home"] == 0


def test_pushed_resources_are_revision_matched_bounded_and_public() -> None:
    """Only matching public resources are sent; large payloads retain HTTP recovery."""
    store = Mock()
    envelope = {
        "revision": 9,
        "payload": {"id_uuid": "match"},
        "history_base": 0,
        "incomplete_revision": 0,
        "id_revisions": {},
        "push_base": 8,
    }
    store.get.return_value = envelope
    assert read_public_match_updates(
        match_id="match",
        revision=9,
        resources=["summary", "tracker"],
        store=store,
    ) == {"summary": {"id_uuid": "match"}}
    store.get.assert_called_once_with(resource_key("match", "summary"))
    assert (
        read_public_match_updates(
            match_id="match",
            revision=10,
            resources=["summary"],
            store=store,
        )
        == {}
    )
    envelope["payload"] = {"large": "x" * (65 * 1024)}
    assert (
        read_public_match_updates(
            match_id="match",
            revision=9,
            resources=["summary"],
            store=store,
        )
        == {}
    )


def test_timeline_push_base_skips_revisions_that_did_not_change_resource() -> None:
    """A viewer need not refetch a timeline after unrelated statistics revisions."""
    graph = create_match_graph(prefix="Push base")
    data = graph.match_data
    data.live_revision = 5
    data.save(update_fields=["live_revision"])
    for revision, resources in [
        (1, ["events"]),
        (2, ["stats"]),
        (3, ["stats"]),
        (4, ["stats"]),
        (5, ["events"]),
    ]:
        MatchLiveChange.objects.create(
            match_data=data,
            revision=revision,
            resources=resources,
            changed_ids={"events": []},
        )
    match_id = str(graph.match.pk)
    prepare_public_match_reads(match_id=match_id)
    payload = read_public_match_updates(
        match_id=match_id,
        revision=5,
        resources=["events"],
        store=published_match_store,
    )["events"]
    assert payload["mode"] == "delta"
    assert payload["base_revision"] == 1
    assert payload["live_revision"] == data.live_revision
