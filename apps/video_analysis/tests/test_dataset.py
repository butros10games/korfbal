"""Image selection must be reversible without approving or resurrecting labels."""

from http import HTTPStatus
import io
import zipfile

from django.contrib.auth.models import User
from django.test import Client
import pytest

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.store import Store, frame_version
from apps.video_analysis.models import Frame, ReviewAudit
from apps.video_analysis.services import dataset
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


def test_remove_undo_and_stale_background_save(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Removed frames leave the authoritative view; stale writes cannot restore them."""
    owner, store, _ = imported
    workspace = Frame.objects.first().recording.workspace
    before = store.read()
    first = dataset.listing(workspace, "unreviewed")["frames"][0]
    original = before["matches"][0]["frames"][0]
    kept = dataset.decide(workspace, owner, dict(first, decision="kept"))
    assert kept["frame"]["frame_version"] == frame_version(original)
    assert store.read()["matches"][0]["frames"][0]["status"] == original["status"]
    removed = dataset.decide(workspace, owner, dict(kept["frame"], decision="removed"))
    assert all(
        f["id"] != first["frame_id"] for f in store.read()["matches"][0]["frames"]
    )
    with store.transaction():
        store._persist(before)
    assert Frame.objects.get(pk=first["id"]).metadata["dataset_decision"] == "removed"
    assert dataset.listing(workspace, "removed")["counts"]["removed"] == 1
    restored = dataset.decide(
        workspace, owner, dict(removed["frame"], decision=removed["previous"])
    )
    assert restored["frame"]["decision"] == "kept"
    assert store.read()["matches"][0]["frames"][0] == original
    assert list(
        ReviewAudit.objects
        .filter(payload__action="dataset")
        .order_by("pk")
        .values_list("payload__decision", flat=True)
    ) == ["kept", "removed", "kept"]


def test_dataset_api_security_and_conflict(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Require MFA/CSRF and reject decisions from an outdated screen."""
    owner, _, _ = imported
    assert (
        Client().get("/video-analysis/dataset").status_code == HTTPStatus.UNAUTHORIZED
    )
    client = verified(owner)
    page = client.get("/video-analysis/dataset").json()
    first, csrf = page["frames"][0], page["csrf"]
    payload = dict(first, decision="removed")
    assert (
        client.post(
            "/video-analysis/dataset", payload, content_type="application/json"
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    response = client.post(
        "/video-analysis/dataset",
        payload,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert response.status_code == HTTPStatus.OK
    assert (
        client.post(
            "/video-analysis/dataset",
            dict(first, decision="kept"),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == HTTPStatus.CONFLICT
    )
    assert (
        client.get("/video-analysis/dataset?decision=removed").json()["frames"][0]["id"]
        == first["id"]
    )
    assert (
        client.get("/video-analysis/dataset?decision=invalid").status_code
        == HTTPStatus.BAD_REQUEST
    )


def test_removed_approved_image_leaves_training_export(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Dataset removal excludes even approved labels from newly generated exports."""
    owner, store, _ = imported
    frame = Frame.objects.select_related("recording").first()
    assert frame is not None
    recording = frame.recording
    recording.metadata = dict(recording.metadata, synthetic=False)
    recording.save(update_fields=["metadata"])
    frame.status, frame.complete = "approved", True
    frame.correction = dict(frame.proposal, ball_visibility="visible")
    frame.save()

    def images() -> list[str]:
        with zipfile.ZipFile(io.BytesIO(store.export())) as archive:
            return [name for name in archive.namelist() if name.startswith("images/")]

    assert len(images()) == 1
    selected = dataset.listing(recording.workspace, "unreviewed")["frames"][0]
    result = dataset.decide(
        recording.workspace, owner, dict(selected, decision="removed")
    )
    assert images() == []
    dataset.decide(recording.workspace, owner, dict(result["frame"], decision="kept"))
    assert len(images()) == 1
