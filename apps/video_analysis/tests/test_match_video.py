"""Match-page video: public only after publishing, synced per tracked period."""

from http import HTTPStatus
from typing import Any
from unittest.mock import patch

from django.test.client import Client
from django.utils import timezone
import pytest

from apps.game_tracker.models import MatchPart
from apps.schedule.tests import match_api_test_support as support
from apps.video_analysis.models import (
    MatchVideoPublication,
    Recording,
    StoredFile,
    Workspace,
)


pytestmark = pytest.mark.django_db

DURATION = 3725.0


class FakeUrls:
    """Sign nothing; return a recognisable URL per recording."""

    def playback_url(self, recording: Recording) -> str:
        """Return a fake signed URL.

        Returns:
            The URL.

        """
        return f"https://media.example.test/{recording.source_id}.mp4?sig=1"


@pytest.fixture(autouse=True)
def urls() -> Any:  # noqa: ANN401 - patch context
    """Avoid object storage in API tests.

    Yields:
        The active patch.

    """
    with patch(
        "apps.schedule.api.match_viewset_video.match_video_urls",
        return_value=FakeUrls(),
    ) as patched:
        yield patched


def link_recording(
    graph: support.MatchGraph, source_id: str = "camera", *, stored: bool = True
) -> Recording:
    """Link a stored recording to the match.

    Returns:
        The recording.

    """
    owner = support.create_user(username=f"owner-{source_id}")
    workspace = Workspace.objects.get_or_create(slug="main", defaults={"owner": owner})[
        0
    ]
    video = f"{source_id}/recording.mp4"
    recording = Recording.objects.create(
        workspace=workspace,
        source_id=source_id,
        match=graph.match,
        metadata={"video": video, "duration_seconds": DURATION},
    )
    if stored:
        StoredFile.objects.create(
            workspace=workspace,
            relative_path=video,
            bucket="media",
            object_key=f"key/{video}",
            sha256="0" * 64,
            size=1,
        )
    return recording


def url(graph: support.MatchGraph) -> str:
    """Return the match video endpoint.

    Returns:
        The path.

    """
    return f"/api/matches/{graph.match.id_uuid}/video/"


def put(client: Client, graph: support.MatchGraph, payload: dict) -> Any:  # noqa: ANN401
    """Send an editor update.

    Returns:
        The response.

    """
    return client.put(url(graph), payload, content_type="application/json")


def test_unpublished_video_is_hidden_from_the_public(client: Client) -> None:
    """Linking a recording for analysis never publishes it by itself."""
    graph = support.create_match_graph(prefix="hidden")
    link_recording(graph)
    response = client.get(url(graph))
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"video": None, "can_edit": False}
    assert response["Cache-Control"] == "private, no-store"


def test_editor_syncs_and_publishes_for_everyone(client: Client) -> None:
    """An editor sees the draft, sets period starts and publishes it."""
    graph = support.create_match_graph(prefix="publish")
    part = support.create_match_part(graph)
    link_recording(graph)
    support.login_coach(client, graph, username="video-coach")
    draft = client.get(url(graph)).json()
    assert draft["can_edit"] is True
    assert draft["video"]["published"] is False
    assert draft["video"]["parts"][0]["video_seconds"] is None
    saved = put(
        client,
        graph,
        {
            "expected_revision": 0,
            "published": True,
            "anchors": {str(part.id_uuid): 754.2},
        },
    )
    assert saved.status_code == HTTPStatus.OK
    video = saved.json()["video"]
    assert (video["published"], video["revision"]) == (True, 1)
    assert video["parts"][0]["video_seconds"] == pytest.approx(754.2)
    public = Client().get(url(graph)).json()
    assert public["can_edit"] is False
    assert public["video"]["url"] == "https://media.example.test/camera.mp4?sig=1"
    assert public["video"]["duration_seconds"] == DURATION
    assert public["video"]["parts"][0]["video_seconds"] == pytest.approx(754.2)


def test_stale_revision_conflicts_and_keeps_saved_state(client: Client) -> None:
    """A second editor with an old revision gets a structured 409."""
    graph = support.create_match_graph(prefix="conflict")
    part = support.create_match_part(graph)
    link_recording(graph)
    support.login_coach(client, graph, username="conflict-coach")
    assert (
        put(client, graph, {"expected_revision": 0, "published": True}).status_code
        == HTTPStatus.OK
    )
    stale = put(
        client,
        graph,
        {"expected_revision": 0, "anchors": {str(part.id_uuid): 10.0}},
    )
    assert stale.status_code == HTTPStatus.CONFLICT
    assert stale.json()["code"] == "revision_conflict"
    assert stale.json()["revision"] == 1
    assert MatchVideoPublication.objects.get().anchors == {}


def test_clearing_an_anchor_and_rejecting_invalid_sync(client: Client) -> None:
    """Anchors stay within the video and on this match's own periods."""
    graph = support.create_match_graph(prefix="validate")
    part = support.create_match_part(graph)
    other = support.create_match_graph(prefix="validate-other")
    foreign = MatchPart.objects.create(
        match_data=other.match_data, part_number=1, start_time=timezone.now()
    )
    link_recording(graph)
    support.login_coach(client, graph, username="validate-coach")
    part_id = str(part.id_uuid)
    assert (
        put(
            client, graph, {"expected_revision": 0, "anchors": {part_id: 5}}
        ).status_code
        == HTTPStatus.OK
    )
    for anchors in ({part_id: DURATION + 1}, {part_id: -1}, {str(foreign.id_uuid): 1}):
        response = put(client, graph, {"expected_revision": 1, "anchors": anchors})
        assert response.status_code == HTTPStatus.BAD_REQUEST
    cleared = put(client, graph, {"expected_revision": 1, "anchors": {part_id: None}})
    assert cleared.json()["video"]["parts"][0]["video_seconds"] is None


def test_recording_breaks_replace_and_validate(client: Client) -> None:
    """Breaks are stored in video order, replaced as a whole and kept in the video."""
    graph = support.create_match_graph(prefix="breaks")
    support.create_match_part(graph)
    link_recording(graph)
    support.login_coach(client, graph, username="breaks-coach")
    assert client.get(url(graph)).json()["video"]["breaks"] == []
    saved = put(
        client,
        graph,
        {
            "expected_revision": 0,
            "breaks": [
                {"video_seconds": 2836.0004, "skipped_seconds": 4},
                {"video_seconds": 914.6, "skipped_seconds": 18.5},
            ],
        },
    )
    assert saved.status_code == HTTPStatus.OK
    stored = [
        {"video_seconds": 914.6, "skipped_seconds": 18.5},
        {"video_seconds": 2836.0, "skipped_seconds": 4.0},
    ]
    assert saved.json()["video"]["breaks"] == stored
    # Other writes keep the breaks; an explicit list replaces them.
    kept = put(client, graph, {"expected_revision": 1, "published": True})
    assert kept.json()["video"]["breaks"] == stored
    for breaks in (
        [{"video_seconds": 0, "skipped_seconds": 1}],
        [{"video_seconds": DURATION + 1, "skipped_seconds": 1}],
        [{"video_seconds": 10, "skipped_seconds": -1}],
        [{"video_seconds": 10, "skipped_seconds": 3601}],
        [
            {"video_seconds": 10, "skipped_seconds": 1},
            {"video_seconds": 10.0001, "skipped_seconds": 2},
        ],
    ):
        response = put(client, graph, {"expected_revision": 2, "breaks": breaks})
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert "breaks" in response.json()
    cleared = put(client, graph, {"expected_revision": 2, "breaks": []})
    assert cleared.json()["video"]["breaks"] == []


@pytest.mark.parametrize("identity", ["anonymous", "plain"])
def test_non_editors_cannot_publish(client: Client, identity: str) -> None:
    """Only the existing event editors may publish or sync."""
    graph = support.create_match_graph(prefix=f"deny-{identity}")
    link_recording(graph)
    if identity == "plain":
        client.force_login(support.create_user(username="plain-viewer"))
    response = put(client, graph, {"expected_revision": 0, "published": True})
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}
    assert not MatchVideoPublication.objects.exists()


def test_match_without_stored_video(client: Client) -> None:
    """A link without a stored file, or no link at all, shows no video."""
    graph = support.create_match_graph(prefix="novideo")
    link_recording(graph, stored=False)
    support.login_coach(client, graph, username="novideo-coach")
    assert client.get(url(graph)).json() == {"video": None, "can_edit": True}
    missing = put(client, graph, {"expected_revision": 0, "published": True})
    assert missing.status_code == HTTPStatus.NOT_FOUND


def test_published_recording_wins_over_newer_draft(client: Client) -> None:
    """A newer unpublished link never replaces the published video."""
    graph = support.create_match_graph(prefix="choose")
    first = link_recording(graph, "first")
    MatchVideoPublication.objects.create(recording=first, published=True)
    link_recording(graph, "second")
    video = client.get(url(graph)).json()["video"]
    assert video["url"].startswith("https://media.example.test/first.mp4")
