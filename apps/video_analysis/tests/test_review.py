"""Native review security, migration and concurrency contracts."""

from http import HTTPStatus
import json
from typing import Any
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.http import HttpResponse
from django.test import Client
import pytest

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine import vision
from apps.video_analysis.engine.store import Store, atomic_json, frame_version
from apps.video_analysis.models import AnalysisJob, Frame, ReviewAudit, Workspace


pytestmark = pytest.mark.django_db


def verified(owner: User) -> Client:
    """Create an MFA-verified session while enforcing CSRF."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    session = client.session
    session["bg_auth_mfa_verified"] = owner.get_session_auth_hash()
    session.save()
    return client


def test_mfa_and_csrf_on_every_resource(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Mfa and csrf on every resource."""
    owner, _, _ = imported
    client = Client()
    for route in [
        "access",
        "state",
        "monitor",
        "media?path=review.json",
        "export",
        "vision",
    ]:
        assert (
            client.get(f"/video-analysis/{route}").status_code
            == HTTPStatus.UNAUTHORIZED
        )
    client.force_login(owner)
    assert client.get("/video-analysis/state").status_code == HTTPStatus.FORBIDDEN
    client = verified(owner)
    assert client.get("/video-analysis/access").json() == {"allowed": True}
    state = client.get("/video-analysis/state").json()
    assert state["matches"]
    assert (
        client.post(
            "/video-analysis/review", "{}", content_type="application/json"
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    assert (
        client.get("/video-analysis/media?path=review.json").status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        client.get("/video-analysis/media?path=../../credentials.env").status_code
        == HTTPStatus.NOT_FOUND
    )
    owner.is_staff = False
    owner.save()
    assert client.get("/video-analysis/state").status_code == HTTPStatus.FORBIDDEN


def test_save_preserves_proposal_and_rejects_same_frame_conflict(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Save preserves proposal and rejects same frame conflict."""
    owner, store, _ = imported
    client = verified(owner)
    state = client.get("/video-analysis/state").json()
    match = state["matches"][0]
    first, second = match["frames"][:2]

    def save(frame: dict[str, Any], notes: str) -> HttpResponse:
        annotation = dict(frame["proposal"], notes=notes)
        return client.post(
            "/video-analysis/review",
            json.dumps({
                "action": "review",
                "match_id": match["id"],
                "frame_id": frame["id"],
                "expected_frame_version": frame["frame_version"],
                "annotation": annotation,
                "complete": True,
                "status": "approved",
            }),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=state["csrf"],
        )

    assert save(first, "Human correction").status_code == HTTPStatus.OK
    assert save(second, "Independent review").status_code == HTTPStatus.OK
    assert save(first, "Stale edit").status_code == HTTPStatus.CONFLICT
    current = store.read()["matches"][0]["frames"][0]
    assert current["proposal"] == first["proposal"]
    assert current["correction"]["notes"] == "Human correction"
    assert len(current["history"]) == 1
    assert ReviewAudit.objects.count() == len([first, second])


def test_ai_import_is_atomic_and_does_not_attribute_labels_to_a_user(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A stale later row rolls back the batch; accepted labels retain AI origin."""
    _, store, _ = imported
    before = store.read()
    match = before["matches"][0]
    rows = [
        {
            "match_id": match["id"],
            "frame_id": frame["id"],
            "frame_version": frame_version(frame),
            "image_sha256": vision.digest(store.media(frame["image"])),
            "annotation": frame["proposal"],
        }
        for frame in match["frames"][:2]
    ]
    report = {
        "provenance": {"kind": "ai", "model": "test", "method": "visual audit"},
        "frames": rows,
    }
    path = store.root / "checked-reviews.json"
    checksum = rows[1]["image_sha256"]
    rows[1]["image_sha256"] = "stale"
    atomic_json(path, report)
    with pytest.raises(ValueError, match="changed after inspection"):
        call_command("import_video_ai_reviews", str(path), workspace="main")
    assert store.read() == before
    assert not ReviewAudit.objects.exists()

    rows[1]["image_sha256"] = checksum
    atomic_json(path, report)
    call_command("import_video_ai_reviews", str(path), workspace="main")
    after = store.read()["matches"][0]["frames"]
    assert after[0]["annotation_provenance"]["kind"] == "ai"
    assert after[0]["correction"]["notes"].startswith("AI-reviewed;")
    assert after[2:] == match["frames"][2:]
    assert ReviewAudit.objects.count() == len(rows)
    assert not ReviewAudit.objects.exclude(actor=None).exists()
    with pytest.raises(ValueError, match="existing review"):
        call_command("import_video_ai_reviews", str(path), workspace="main")


def test_import_idempotence_and_media_ranges(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Import idempotence and media ranges."""
    owner, store, legacy = imported
    call_command("import_video_reviews", str(legacy.root), owner=owner.username)
    assert Workspace.objects.count() == 1
    assert Frame.objects.count() == len(legacy.read()["matches"][0]["frames"])
    client = verified(owner)
    frame = store.read()["matches"][0]["frames"][0]
    with (
        patch.object(
            DatabaseStore, "read", side_effect=AssertionError("Full review read")
        ),
        patch(
            "apps.video_analysis.adapters.objects.WorkspaceObjects.hydrate_artifacts",
            side_effect=AssertionError("Artifact hydration"),
        ),
    ):
        response = client.get(
            "/video-analysis/media", {"path": frame["image"]}, HTTP_RANGE="bytes=0-9"
        )
    assert response.status_code == HTTPStatus.PARTIAL_CONTENT
    assert (
        b"".join(response.streaming_content)
        == store.media(frame["image"]).read_bytes()[:10]
    )
    assert (
        client.get(
            "/video-analysis/media",
            {"path": frame["image"]},
            HTTP_RANGE="bytes=999999999-",
        ).status_code
        == HTTPStatus.RANGE_NOT_SATISFIABLE
    )


def test_analysis_is_durable_and_never_runs_in_request(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Analysis is durable and never runs in request."""
    owner, _, _ = imported
    client = verified(owner)
    state = client.get("/video-analysis/state").json()
    with patch("apps.video_analysis.services.jobs.enqueue") as enqueue:
        response = client.post(
            "/video-analysis/analyze",
            json.dumps({
                "match_id": state["matches"][0]["id"],
                "frame_id": state["matches"][0]["frames"][0]["id"],
            }),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=state["csrf"],
        )
    assert response.status_code == HTTPStatus.ACCEPTED
    assert AnalysisJob.objects.get().status == "queued"
    assert enqueue.call_args.kwargs["queue"] == "vision"
    assert client.get("/video-analysis/job").json()["running"] is True
    assert "payload" not in client.get("/video-analysis/monitor").content.decode()


def test_latest_drafts_span_batches_without_overwriting_reviews(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """All technical batches are discoverable; stale and approved frames stay out."""
    owner, store, _ = imported
    match = store.read()["matches"][0]
    frames = match["frames"][:2]
    for index, frame in enumerate(frames):
        root = store.root / "vision/runs" / f"batch-{index}"
        atomic_json(
            root / "run.json",
            {
                "id": root.name,
                "kind": "propose",
                "status": "completed",
                "created_at": index,
            },
        )
        atomic_json(
            root / "predictions.json",
            {
                "frames": [
                    {
                        "match_id": match["id"],
                        "frame_id": frame["id"],
                        "frame_version": frame_version(frame),
                        "priority": 0,
                        "reasons": [],
                        "prediction": {"objects": []},
                        "suggestion": frame["proposal"],
                    }
                ]
            },
        )
    client = verified(owner)
    queue = client.get("/video-analysis/vision/queue?run=latest").json()["frames"]
    assert len(queue) == len(frames)
    for frame in frames:
        assert vision.read_prediction(store, "latest", match["id"], frame["id"])[
            "prediction"
        ] == {"objects": []}
    Frame.objects.filter(source_id=frames[0]["id"]).update(status="approved")
    assert len(vision.review_queue(store, "latest")) == len(frames) - 1


def test_swapping_draft_teams_changes_only_open_model_drafts(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """One switch per match fixes which shirt group is the first club."""
    owner, _, _ = imported
    client = verified(owner)
    state = client.get("/video-analysis/state").json()
    match = state["matches"][0]
    draft = {
        "scene": "live",
        "event": "none",
        "notes": "",
        "objects": [
            {"label": "player", "bbox": [0.1, 0.1, 0.1, 0.3], "team": "team_a"},
            {"label": "player", "bbox": [0.4, 0.1, 0.1, 0.3], "team": "team_b"},
            {"label": "player", "bbox": [0.6, 0.1, 0.1, 0.3], "team": "unknown"},
            {"label": "referee", "bbox": [0.8, 0.1, 0.1, 0.3], "team": "unknown"},
        ],
    }
    frames = Frame.objects.filter(recording__source_id=match["id"]).order_by("position")
    for frame in frames:
        frame.proposal = draft
        frame.save(update_fields=["proposal"])
    reviewed = frames[0]
    reviewed.correction = draft
    reviewed.status = "approved"
    reviewed.save(update_fields=["correction", "status"])
    revision = Workspace.objects.get().revision

    def swap(current: int) -> HttpResponse:
        return client.post(
            "/video-analysis/review",
            json.dumps({
                "action": "swap_teams",
                "match_id": match["id"],
                "revision": current,
            }),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=state["csrf"],
        )

    response = swap(revision)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["swapped"] == frames.count() - 1
    teams = [o["team"] for o in Frame.objects.get(pk=frames[1].pk).proposal["objects"]]
    assert teams == ["team_b", "team_a", "unknown", "unknown"]
    # Reviewed work already names its clubs; it is never touched.
    kept = Frame.objects.get(pk=reviewed.pk)
    assert kept.proposal == draft
    assert kept.correction == draft
    assert swap(revision).status_code == HTTPStatus.CONFLICT
