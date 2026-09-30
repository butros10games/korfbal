"""Tags, notes and clips on the match video: who sees them and what is valid."""

from http import HTTPStatus
from typing import Any

from django.test.client import Client
import pytest

from apps.schedule.tests import match_api_test_support as support
from apps.video_analysis.models import MatchVideoAnnotation, MatchVideoPublication
from apps.video_analysis.tests.test_match_video import DURATION, link_recording


pytestmark = pytest.mark.django_db
JSON = "application/json"


def url(graph: support.MatchGraph, annotation_id: str | None = None) -> str:
    """Return the annotations endpoint, or one annotation's.

    Returns:
        The path.

    """
    base = f"/api/matches/{graph.match.id_uuid}/video/annotations/"
    return f"{base}{annotation_id}/" if annotation_id else base


def post(client: Client, graph: support.MatchGraph, payload: dict) -> Any:  # noqa: ANN401
    """Add an annotation as the logged-in user.

    Returns:
        The response.

    """
    return client.post(url(graph), payload, content_type=JSON)


def test_editors_annotate_and_viewers_see_only_published_viewer_annotations(
    client: Client,
) -> None:
    """Private coach notes stay private; shared ones follow the video's publication."""
    graph = support.create_match_graph(prefix="annotate")
    recording = link_recording(graph)
    support.login_coach(client, graph, username="annotate-coach")
    tag = post(client, graph, {"kind": "tag", "label": "Rebound", "start_seconds": 60})
    assert tag.status_code == HTTPStatus.CREATED
    assert tag.json()["annotation"]["label"] == "Rebound"
    clip = post(
        client,
        graph,
        {
            "kind": "clip",
            "label": "Goede aanval",
            "start_seconds": 100,
            "end_seconds": 130.5,
        },
    )
    assert clip.status_code == HTTPStatus.CREATED
    private = post(
        client,
        graph,
        {
            "kind": "note",
            "body": "Sam staat te ver weg",
            "start_seconds": 200,
            "visibility": "editors",
            "drawing": [
                {
                    "type": "arrow",
                    "points": [[0.1, 0.2], [0.5, 0.5]],
                    "color": "#FF0000",
                }
            ],
        },
    )
    assert private.status_code == HTTPStatus.CREATED
    assert private.json()["annotation"]["drawing"][0]["color"] == "#ff0000"

    editor_view = client.get(url(graph)).json()
    assert editor_view["can_edit"] is True
    assert [row["kind"] for row in editor_view["annotations"]] == [
        "tag",
        "clip",
        "note",
    ]

    public = Client()
    assert public.get(url(graph)).json() == {"annotations": [], "can_edit": False}
    MatchVideoPublication.objects.create(recording=recording, published=True)
    shared = public.get(url(graph)).json()["annotations"]
    assert [row["kind"] for row in shared] == ["tag", "clip"]
    assert shared[0]["author"] == "annotate-coach"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"kind": "clip", "start_seconds": 10, "end_seconds": 5}, "ends after"),
        ({"kind": "clip", "start_seconds": 10, "end_seconds": 700}, "ten minutes"),
        ({"kind": "tag", "start_seconds": 10}, "needs a label"),
        ({"kind": "note", "start_seconds": 10}, "text or a drawing"),
        ({"kind": "tag", "label": "x", "start_seconds": DURATION + 1}, "within"),
        (
            {"kind": "tag", "label": "x", "start_seconds": 1, "end_seconds": 2},
            "Only clips",
        ),
        (
            {"kind": "tag", "label": "x", "start_seconds": 1, "extra": 1},
            "Unknown fields",
        ),
        ({"kind": "bogus", "start_seconds": 1}, "kind must be"),
        ({"kind": [], "start_seconds": 1}, "kind must be"),
        ({"kind": {}, "start_seconds": 1}, "kind must be"),
        (
            {"kind": "tag", "label": "x", "start_seconds": 1, "visibility": []},
            "visibility must be",
        ),
        (
            {"kind": "tag", "label": "x", "start_seconds": 1, "visibility": {}},
            "visibility must be",
        ),
        ({"kind": "tag", "label": "x"}, "required"),
        (
            {"kind": "note", "start_seconds": 1, "drawing": [{"type": "star"}]},
            "Unknown drawing shape",
        ),
        (
            {"kind": "note", "start_seconds": 1, "drawing": [{"type": []}]},
            "Unknown drawing shape",
        ),
        (
            {"kind": "note", "start_seconds": 1, "drawing": [{"type": {}}]},
            "Unknown drawing shape",
        ),
        (
            {
                "kind": "note",
                "start_seconds": 1,
                "drawing": [{"type": "line", "points": [[0, 0], [2, 2]]}],
            },
            "fractions",
        ),
        (
            {"kind": "tag", "label": "x", "start_seconds": 1, "player_ids": ["nope"]},
            "player IDs",
        ),
    ],
)
def test_invalid_annotations_are_refused(
    client: Client, payload: dict, message: str
) -> None:
    """Every rule answers with a readable 400."""
    graph = support.create_match_graph(prefix="annotate-invalid")
    link_recording(graph)
    support.login_coach(client, graph, username="invalid-coach")
    response = post(client, graph, payload)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert message in response.json()["detail"][0]
    assert not MatchVideoAnnotation.objects.exists()


def test_editors_change_and_remove_annotations_of_their_match_only(
    client: Client,
) -> None:
    """Changes keep omitted fields; another match's annotation cannot be reached."""
    graph = support.create_match_graph(prefix="annotate-edit")
    other = support.create_match_graph(prefix="annotate-other")
    link_recording(graph)
    link_recording(other, source_id="other")
    support.login_coach(client, graph, username="edit-coach")
    created = post(
        client, graph, {"kind": "tag", "label": "Assist", "start_seconds": 5}
    )
    annotation_id = created.json()["annotation"]["id"]
    changed = client.patch(
        url(graph, annotation_id), {"label": "Goede assist"}, content_type=JSON
    )
    assert changed.status_code == HTTPStatus.OK
    assert changed.json()["annotation"]["label"] == "Goede assist"
    assert changed.json()["annotation"]["start_seconds"] == pytest.approx(5)
    assert (
        client.patch(url(other, annotation_id), {"label": "x"}, content_type=JSON)
    ).status_code in {HTTPStatus.NOT_FOUND, HTTPStatus.FORBIDDEN}
    assert client.delete(url(graph, annotation_id)).status_code == HTTPStatus.NO_CONTENT
    assert not MatchVideoAnnotation.objects.exists()


@pytest.mark.parametrize("identity", ["anonymous", "plain"])
def test_non_editors_cannot_annotate(client: Client, identity: str) -> None:
    """Only the match's editors write annotations."""
    graph = support.create_match_graph(prefix=f"annotate-deny-{identity}")
    link_recording(graph)
    if identity == "plain":
        client.force_login(support.create_user(username="annotate-viewer"))
    response = post(client, graph, {"kind": "tag", "label": "x", "start_seconds": 1})
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}
    assert not MatchVideoAnnotation.objects.exists()
