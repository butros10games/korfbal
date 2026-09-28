"""Canonical match associations preserve video review and tracker state."""

from http import HTTPStatus

from django.contrib.auth.models import User
from django.test import Client
import pytest

from apps.schedule.models import Match
from apps.schedule.tests.match_api_test_support import create_match_graph
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import Recording, Workspace
from apps.video_analysis.services import match_links
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


def test_link_unlink_and_stale_write_preserve_metadata(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """MFA/CSRF-protected edits target only the FK and reject stale replacement."""
    owner, store, _ = imported
    graph = create_match_graph(prefix="link")
    recording = Recording.objects.get(source_id="demo")
    recording.metadata["video"] = "demo/recording.mp4"
    recording.save(update_fields=["metadata"])
    before = recording.metadata.copy()
    client = verified(owner)
    csrf = client.get("/video-analysis/state?scope=recording&match=demo").json()["csrf"]
    payload = {
        "recording_id": "demo",
        "match_id": str(graph.match.pk),
        "expected_match_id": None,
    }
    response = client.post(
        "/video-analysis/match-link",
        payload,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["linked_match"]["id"] == str(graph.match.pk)
    for url in [
        "/video-analysis/state",
        "/video-analysis/state?scope=recording&match=demo",
    ]:
        assert client.get(url).json()["matches"][0]["linked_match"]["id"] == str(
            graph.match.pk
        )
    assert (
        client.post(
            "/video-analysis/match-link",
            {**payload, "match_id": None},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == HTTPStatus.CONFLICT
    )
    recording.refresh_from_db()
    assert recording.metadata == before
    assert (
        client.post(
            "/video-analysis/match-link",
            {**payload, "match_id": None, "expected_match_id": str(graph.match.pk)},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == HTTPStatus.OK
    )
    recording.refresh_from_db()
    assert recording.match_id is None
    assert recording.metadata == before
    assert store.read()["matches"][0]["video"] == before["video"]


@pytest.mark.parametrize(
    "target", ["bad-id", "10000000-0000-4000-8000-000000000099", 123]
)
def test_invalid_match_and_missing_recording_are_rejected(
    imported: tuple[User, DatabaseStore, Store], target: object
) -> None:
    """Reject forged references before publishing changes."""
    _owner, _, _ = imported
    recording = Recording.objects.get(source_id="demo")
    recording.metadata["video"] = "demo/recording.mp4"
    recording.save(update_fields=["metadata"])
    w = Workspace.objects.get(slug="main")
    with pytest.raises(ValueError, match="match"):
        match_links.save(
            w, {"recording_id": "demo", "match_id": target, "expected_match_id": None}
        )
    with pytest.raises(ValueError, match="recording"):
        match_links.save(
            w, {"recording_id": "unknown", "match_id": None, "expected_match_id": None}
        )
    w.refresh_from_db()
    assert w.revision == 1


def test_search_matches_both_teams_and_is_bounded(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Search is server-side and blank searches never drain the catalogue."""
    graph = create_match_graph(prefix="search")
    graph.home_team.name = "2"
    graph.home_team.club.name = "Noord"
    graph.home_team.club.save(update_fields=["name"])
    graph.home_team.save(update_fields=["name"])
    graph.away_team.name = "3"
    graph.away_team.club.name = "Zuid"
    graph.away_team.club.save(update_fields=["name"])
    graph.away_team.save(update_fields=["name"])
    assert match_links.search("")["matches"] == []
    matches = match_links.search("noord zuid")["matches"]
    assert [m["id"] for m in matches] == [str(graph.match.pk)]
    assert matches[0]["title"] == "Noord 2 - Zuid 3"
    assert match_links.search("missing")["matches"] == []
    for _ in range(match_links.PAGE_SIZE):
        Match.objects.create(
            home_team=graph.home_team,
            away_team=graph.away_team,
            season=graph.match.season,
            start_time=graph.match.start_time,
        )
    page = match_links.search("Noord")
    assert len(page["matches"]) == match_links.PAGE_SIZE
    assert page["has_more"] is True


def test_match_link_requires_mfa_and_csrf(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The new routes retain the administrative review boundary."""
    owner, _, _ = imported
    client = Client(enforce_csrf_checks=True)
    assert (
        client.get("/video-analysis/matches?q=Noord").status_code
        == HTTPStatus.UNAUTHORIZED
    )
    client.force_login(owner)
    assert (
        client.get("/video-analysis/matches?q=Noord").status_code
        == HTTPStatus.FORBIDDEN
    )
    assert (
        verified(owner)
        .post("/video-analysis/match-link", {}, content_type="application/json")
        .status_code
        == HTTPStatus.FORBIDDEN
    )
