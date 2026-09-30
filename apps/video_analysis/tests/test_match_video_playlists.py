"""Playlists of saved clips: who sees them and which clips they hold."""

from http import HTTPStatus
from typing import Any

from django.test.client import Client
import pytest

from apps.schedule.tests import match_api_test_support as support
from apps.video_analysis.models import MatchVideoPlaylist, MatchVideoPublication
from apps.video_analysis.tests.test_match_video import link_recording


pytestmark = pytest.mark.django_db
JSON = "application/json"


def url(graph: support.MatchGraph, playlist_id: str | None = None) -> str:
    """Return the playlists endpoint, or one playlist's.

    Returns:
        The path.

    """
    base = f"/api/matches/{graph.match.id_uuid}/video/playlists/"
    return f"{base}{playlist_id}/" if playlist_id else base


def clip(client: Client, graph: support.MatchGraph, **fields: object) -> str:
    """Save a clip and return its ID.

    Returns:
        The clip's ID.

    """
    response = client.post(
        f"/api/matches/{graph.match.id_uuid}/video/annotations/",
        {"kind": "clip", "label": "Aanval", "start_seconds": 10, "end_seconds": 20}
        | fields,
        content_type=JSON,
    )
    assert response.status_code == HTTPStatus.CREATED
    return str(response.json()["annotation"]["id"])


def post(client: Client, graph: support.MatchGraph, payload: dict) -> Any:  # noqa: ANN401
    """Add a playlist as the logged-in user.

    Returns:
        The response.

    """
    return client.post(url(graph), payload, content_type=JSON)


def test_playlists_keep_order_and_hide_private_clips_from_viewers(
    client: Client,
) -> None:
    """Viewers see shared playlists without the editors-only clips in them."""
    graph = support.create_match_graph(prefix="playlist")
    recording = link_recording(graph)
    support.login_coach(client, graph, username="playlist-coach")
    first = clip(client, graph)
    private = clip(
        client, graph, start_seconds=40, end_seconds=50, visibility="editors"
    )
    last = clip(client, graph, start_seconds=60, end_seconds=70)
    created = post(
        client,
        graph,
        {"title": " Aanvallen thuis ", "annotation_ids": [last, private, first]},
    )
    assert created.status_code == HTTPStatus.CREATED
    playlist = created.json()["playlist"]
    assert playlist["title"] == "Aanvallen thuis"
    assert playlist["annotation_ids"] == [last, private, first]

    public = Client()
    assert public.get(url(graph)).json()["playlists"] == []
    MatchVideoPublication.objects.create(recording=recording, published=True)
    shared = public.get(url(graph)).json()["playlists"]
    assert shared[0]["annotation_ids"] == [last, first]


def test_playlist_changes_and_removed_clips(client: Client) -> None:
    """A removed clip drops out; a changed playlist keeps omitted fields."""
    graph = support.create_match_graph(prefix="playlist-edit")
    link_recording(graph)
    support.login_coach(client, graph, username="playlist-editor")
    first = clip(client, graph)
    second = clip(client, graph, start_seconds=30, end_seconds=35)
    playlist_id = post(
        client, graph, {"title": "Reel", "annotation_ids": [first, second]}
    ).json()["playlist"]["id"]
    renamed = client.patch(
        url(graph, playlist_id), {"title": "Beste acties"}, content_type=JSON
    )
    assert renamed.json()["playlist"]["annotation_ids"] == [first, second]
    client.delete(f"/api/matches/{graph.match.id_uuid}/video/annotations/{first}/")
    assert client.get(url(graph)).json()["playlists"][0]["annotation_ids"] == [second]
    assert client.delete(url(graph, playlist_id)).status_code == HTTPStatus.NO_CONTENT
    assert not MatchVideoPlaylist.objects.exists()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"title": "", "annotation_ids": ["x"]}, "needs a title"),
        ({"title": "Reel", "annotation_ids": []}, "one to a hundred"),
        ({"title": "Reel", "annotation_ids": ["nope"]}, "clip IDs"),
        (
            {
                "title": "Reel",
                "annotation_ids": ["00000000-0000-4000-8000-000000000000"],
            },
            "clip on this video",
        ),
        ({"title": "Reel"}, "required"),
    ],
)
def test_invalid_playlists_are_refused(
    client: Client, payload: dict, message: str
) -> None:
    """Every rule answers with a readable 400."""
    graph = support.create_match_graph(prefix="playlist-invalid")
    link_recording(graph)
    support.login_coach(client, graph, username="playlist-invalid-coach")
    response = post(client, graph, payload)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert message in response.json()["detail"][0]


def test_tags_cannot_join_a_playlist_and_viewers_cannot_add_one(
    client: Client,
) -> None:
    """Playlists hold clips only, and only editors write them."""
    graph = support.create_match_graph(prefix="playlist-rules")
    link_recording(graph)
    support.login_coach(client, graph, username="playlist-rules-coach")
    tag = client.post(
        f"/api/matches/{graph.match.id_uuid}/video/annotations/",
        {"kind": "tag", "label": "Rebound", "start_seconds": 5},
        content_type=JSON,
    ).json()["annotation"]["id"]
    assert (
        post(client, graph, {"title": "Reel", "annotation_ids": [tag]}).status_code
        == HTTPStatus.BAD_REQUEST
    )
    denied = post(Client(), graph, {"title": "Reel", "annotation_ids": [tag]})
    assert denied.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}


@pytest.mark.parametrize("visibility", [[], {}])
def test_invalid_visibility_returns_a_client_error(
    client: Client, visibility: object
) -> None:
    """Malformed choice values cannot turn an editor write into a server error."""
    graph = support.create_match_graph(prefix="playlist-visibility")
    link_recording(graph)
    support.login_coach(client, graph, username="playlist-visibility-coach")
    response = post(
        client,
        graph,
        {
            "title": "Reel",
            "annotation_ids": [clip(client, graph)],
            "visibility": visibility,
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert not MatchVideoPlaylist.objects.exists()
