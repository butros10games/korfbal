"""Persistent preparation boundaries, retry safety and human-only review approval."""

from http import HTTPStatus
from unittest.mock import patch
import uuid

from django.contrib.auth.models import User
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.store import (
    ConflictError,
    Store,
    atomic_json,
    frame_version,
)
from apps.video_analysis.models import (
    AnalysisJob,
    ClipReview,
    Frame,
    Recording,
    ReviewPipeline,
    Workspace,
)
from apps.video_analysis.queries import frame_payload
from apps.video_analysis.services import pipeline, pipeline_worker
from apps.video_analysis.services.pipeline_corrections import clip_drafts
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


@pytest.fixture
def prepared(
    imported: tuple[User, DatabaseStore, Store],
) -> tuple[User, DatabaseStore, Workspace]:
    """Provide a recording and completed all-class model without real inference."""
    owner, store, _ = imported
    recording = Recording.objects.get(source_id="demo")
    recording.metadata.update(
        video="demo/recording.mp4",
        duration_seconds=60,
        video_sha256="frozen-video",
        active_periods=[{"start": 0, "end": 21}, {"start": 40, "end": 60}],
    )
    recording.save()
    atomic_json(
        store.root / "vision/runs/model/run.json",
        {
            "kind": "train",
            "status": "completed",
            "classes": ["player", "referee", "ball", "basket"],
        },
    )
    return owner, store, Workspace.objects.get(slug="main")


def request() -> dict:
    """Return a fresh, explicitly pinned preparation request."""
    return {
        "request_id": str(uuid.uuid4()),
        "match_id": "demo",
        "interval": 10,
        "model": "model",
    }


def test_plan_covers_play_without_breaks_or_tiny_tail() -> None:
    """Every tracking interval stays in active play, including a fractional tail."""
    recipe = pipeline.plan([{"start": 10, "end": 40.1}, {"start": 100, "end": 130}], 10)
    assert recipe["times"] == [10, 20, 30, 40, 100, 110, 120]
    assert sum(c["duration"] for c in recipe["clips"]) == pytest.approx(60.1)
    assert all(1 <= c["duration"] <= pipeline.CLIP_SECONDS for c in recipe["clips"])
    assert all(
        c["start"] + c["duration"] <= recipe["times"][3] + 0.1
        or c["start"] >= recipe["times"][4]
        for c in recipe["clips"]
    )


def test_idempotency_and_frozen_cut(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """An HTTP retry returns its original recipe even if the timeline later changes."""
    owner, store, workspace = prepared
    payload = request()
    first = pipeline.submit(workspace, owner, store, payload)
    Recording.objects.filter(source_id="demo").update(metadata={"video": "changed"})
    assert pipeline.submit(workspace, owner, store, payload).pk == first.pk
    assert ReviewPipeline.objects.count() == 1
    with pytest.raises(ConflictError):
        pipeline.submit(workspace, owner, store, {**payload, "interval": 20})
    assert first.recipe["periods"][0] == {"start": 0, "end": 21}


def test_save_cut_and_enqueue_are_atomic(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """An incompatible checkpoint rolls back the cut and preparation together."""
    owner, store, workspace = prepared
    original = Recording.objects.get(source_id="demo").metadata.copy()
    atomic_json(
        store.root / "vision/runs/bad/run.json",
        {"kind": "train", "status": "completed", "classes": ["player"]},
    )
    payload = {
        **request(),
        "revision": workspace.revision,
        "active_periods": [{"start": 2, "end": 30}],
        "model": "bad",
    }
    with pytest.raises(ValueError, match="completed model"):
        pipeline.submit(workspace, owner, store, payload)
    assert Recording.objects.get(source_id="demo").metadata == original
    assert not ReviewPipeline.objects.exists()


def test_import_queue_accepts_backlog_and_rejects_arbitrary_urls(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """Adding a second recording does not depend on current worker capacity."""
    owner, store, workspace = prepared
    for number in range(2):
        pipeline.submit(
            workspace,
            owner,
            store,
            {
                "request_id": str(uuid.uuid4()),
                "source_url": f"https://eyecons.com/videos/match-{number}",
            },
        )
    assert ReviewPipeline.objects.count() == len(range(2))
    for url in [
        "http://localhost/a.mp4",
        "https://eyecons.com@localhost/videos/a",
        "https://eyecons.com/videos/a?redirect=private",
    ]:
        with pytest.raises(ValueError, match="Use an https"):
            pipeline.submit(
                workspace,
                owner,
                store,
                {"request_id": str(uuid.uuid4()), "source_url": url},
            )


def test_worker_yields_to_manual_jobs_and_honors_pause(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """Queued work waits rather than competing; a pause survives a unit finishing."""
    owner, store, workspace = prepared
    run = pipeline.submit(workspace, owner, store, request())
    job = AnalysisJob.objects.create(
        workspace=workspace, kind="sample", status="running"
    )
    assert pipeline_worker.claim(str(workspace.pk)) is None
    job.status = "completed"
    job.save()

    def unit(current: ReviewPipeline, _store: Store) -> tuple[dict, bool]:
        pipeline.control(
            workspace,
            {"id": str(current.pk), "revision": current.revision, "action": "pause"},
        )
        return {"frames_done": 5}, False

    with patch.object(pipeline_worker, "perform", side_effect=unit):
        pipeline_worker.advance(str(workspace.pk))
    run.refresh_from_db()
    assert run.status == "paused"
    assert run.progress == {
        "frames_done": 5,
        "pause_requested": run.progress["pause_requested"],
    }
    pipeline.control(
        workspace, {"id": str(run.pk), "revision": run.revision, "action": "resume"}
    )
    run.refresh_from_db()
    assert run.status == "queued"
    assert run.progress == {
        "frames_done": 5,
        "pause_requested": run.progress["pause_requested"],
    }


def test_failed_unit_retains_cursor_for_retry(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """Errors do not silently advance progress or discard completed batches."""
    owner, store, workspace = prepared
    run = pipeline.submit(workspace, owner, store, request())
    run.progress = {"frames_done": 25}
    run.save()
    with patch.object(
        pipeline_worker, "perform", side_effect=RuntimeError("worker failure")
    ):
        pipeline_worker.advance(str(workspace.pk))
    run.refresh_from_db()
    assert run.status == "failed"
    assert run.progress == {"frames_done": 25}
    assert "worker failure" not in run.message


def test_proposals_never_replace_human_work(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """Background drafts attach only to the exact untouched input frame version."""
    owner, store, workspace = prepared
    run = pipeline.submit(workspace, owner, store, request())
    frames = list(Frame.objects.all()[:3])
    records = []
    for frame in frames:
        frame.proposal = None
        frame.save()
        records.append({
            "id": frame.source_id,
            "frame_version": frame_version(frame_payload(frame)),
            "prediction": {"scene": "live", "objects": []},
        })
    frames[0].correction = {"scene": "live", "objects": []}
    frames[0].status = "approved"
    frames[0].complete = True
    frames[0].save()
    frames[1].metadata = {**frames[1].metadata, "dataset_decision": "removed"}
    frames[1].save()
    pipeline_worker.publish_proposals(
        run, {"frames": records}, "vision/pipeline/draft.json"
    )
    for frame in frames:
        frame.refresh_from_db()
    assert frames[0].proposal is None
    assert frames[0].complete
    assert frames[1].proposal is None
    assert frames[2].proposal == records[2]["prediction"]
    assert frames[2].correction is None
    assert not frames[2].complete
    assert frames[2].status == "pending"
    # Replaying publication is harmless.
    revision = Workspace.objects.get(pk=workspace.pk).revision
    pipeline_worker.publish_proposals(
        run, {"frames": records}, "vision/pipeline/draft.json"
    )
    assert Workspace.objects.get(pk=workspace.pk).revision == revision


def make_clip(prepared: tuple[User, DatabaseStore, Workspace]) -> ClipReview:
    """Create an immutable completed tracking run awaiting inspection."""
    owner, store, workspace = prepared
    run = pipeline.submit(workspace, owner, store, request())
    job = AnalysisJob.objects.create(
        workspace=workspace,
        kind="clip",
        status="completed",
        payload={"match_id": "demo", "options": {"start": 0, "duration": 21}},
    )
    return ClipReview.objects.create(pipeline=run, job=job)


def test_clip_verdict_is_versioned_and_does_not_approve_frames(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """Watching a clip is not a complete frame annotation or a training negative."""
    owner, _, workspace = prepared
    clip = make_clip(prepared)
    payload = {"id": clip.pk, "revision": 0, "status": "approved"}
    with pytest.raises(ValueError, match="entire clip"):
        pipeline.review_clip(workspace, owner, payload)
    pipeline.review_clip(workspace, owner, {**payload, "watched_complete": True})
    with pytest.raises(ConflictError):
        pipeline.review_clip(workspace, owner, {**payload, "watched_complete": True})
    clip.refresh_from_db()
    assert clip.status == "approved"
    assert len(clip.history) == 1
    assert not Frame.objects.filter(complete=True).exists()
    assert not pipeline.listing(workspace)["clips"]


def test_clip_correction_stays_inside_clip(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """A correction sequence keeps the model/source and never crosses the cut."""
    owner, store, workspace = prepared
    clip = make_clip(prepared)
    run = pipeline.submit(
        workspace,
        owner,
        store,
        {"request_id": str(uuid.uuid4()), "clip_review_id": clip.pk, "seconds": 0},
    )
    assert run.recipe["stage"] == "correction"
    assert run.recipe["plan"]["times"] == [0, 0.08, 0.16, 0.24, 0.32]
    assert run.recipe["plan"]["clips"] == []
    assert run.recipe["model"] == "model"


def test_pipeline_api_requires_mfa_and_scopes_clip_links(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """Direct clip links stay within the authenticated configured workspace."""
    owner, _, _workspace = prepared
    client = verified(owner)
    assert client.get("/video-analysis/pipeline").status_code == HTTPStatus.OK
    assert "csrf" in client.get("/video-analysis/pipeline").json()
    clip = make_clip(prepared)
    assert (
        client.get(f"/video-analysis/pipeline/clip?id={clip.pk}").status_code
        == HTTPStatus.OK
    )
    other = Workspace.objects.create(slug="other", owner=owner)
    ReviewPipeline.objects.filter(pk=clip.pipeline_id).update(workspace=other)
    assert (
        client.get(f"/video-analysis/pipeline/clip?id={clip.pk}").status_code
        == HTTPStatus.NOT_FOUND
    )
    client.logout()
    assert client.get("/video-analysis/pipeline").status_code == HTTPStatus.UNAUTHORIZED


def test_pipeline_advances_all_units_and_retains_clip_attempts(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """Exercise the queue through extraction, drafts and clip inspection."""
    owner, store, workspace = prepared
    run = pipeline.submit(workspace, owner, store, request())

    def extract(_store: Store, _match: dict, times: list[float]) -> list[dict]:
        return [
            {
                "id": f"at-{round(time * 1000):09d}",
                "time_seconds": time,
                "image": "demo/court.svg",
            }
            for time in times
        ]

    def infer(_store: Store, _model: str, frames: list[dict], _output: str) -> dict:
        return {
            "frames": [
                {**frame, "prediction": {"scene": "live", "objects": []}}
                for frame in frames
            ]
        }

    def clip(_store: Store, run_id: str, _payload: dict) -> None:
        atomic_json(
            store.root / "vision/clips" / run_id / "run.json",
            {"status": "completed", "chunks": []},
        )

    with (
        patch.object(pipeline_worker, "extract_pipeline_frames", side_effect=extract),
        patch.object(pipeline_worker, "infer_pipeline_batch", side_effect=infer),
        patch.object(pipeline_worker, "run_clip", side_effect=clip),
    ):
        for _ in range(3):
            pipeline_worker.advance(str(workspace.pk))
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.progress["frames_done"] == len(run.recipe["plan"]["times"])
    assert ClipReview.objects.filter(pipeline=run).count() == len(
        run.recipe["plan"]["clips"]
    )
    sampled = Frame.objects.filter(source_id__startswith="at-")
    assert all(frame.proposal is not None for frame in sampled)
    assert not sampled.filter(complete=True).exists()
    assert not sampled.exclude(status="pending").exists()
    before = AnalysisJob.objects.count()
    pipeline_worker.advance(str(workspace.pk))
    assert AnalysisJob.objects.count() == before


def test_clip_retry_preserves_failed_attempt_and_reuses_completed_one(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """A retry gets a fresh immutable clip ID; completed work is never repeated."""
    owner, store, workspace = prepared
    run = pipeline.submit(workspace, owner, store, request())
    recording = Recording.objects.get(source_id="demo")
    section = run.recipe["plan"]["clips"][0]
    with (
        patch.object(
            pipeline_worker, "run_clip", side_effect=RuntimeError("interrupted")
        ),
        pytest.raises(RuntimeError, match="interrupted"),
    ):
        pipeline_worker.prepare_clip(run, store, recording, section, 0)
    failed = AnalysisJob.objects.get()
    assert failed.status == "failed"

    def clip(_store: Store, run_id: str, _payload: dict) -> None:
        atomic_json(
            store.root / "vision/clips" / run_id / "run.json",
            {"status": "completed", "chunks": []},
        )

    with patch.object(pipeline_worker, "run_clip", side_effect=clip) as inference:
        pipeline_worker.prepare_clip(run, store, recording, section, 0)
        pipeline_worker.prepare_clip(run, store, recording, section, 0)
    inference.assert_called_once()
    assert ClipReview.objects.get().job_id != failed.pk
    assert AnalysisJob.objects.filter(pk=failed.pk, status="failed").exists()


def test_old_clip_still_opens_beyond_recent_history(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """A queued review stays playable after many newer clips are generated."""
    owner, _store, workspace = prepared
    reviewed = make_clip(prepared)
    for _ in range(51):
        AnalysisJob.objects.create(
            workspace=workspace, kind="clip", payload=reviewed.job.payload
        )
    client = verified(owner)
    result = client.get(f"/video-analysis/clips?run={reviewed.job_id}")
    assert result.status_code == HTTPStatus.OK
    assert str(reviewed.job_id) in {row["id"] for row in result.json()["runs"]}


def test_capacity_wait_and_exhausted_worker_retry_are_actionable(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """A full disk waits; an exhausted execution envelope can be resumed explicitly."""
    owner, store, workspace = prepared
    run = pipeline.submit(workspace, owner, store, request())
    with (
        patch.object(pipeline_worker, "pipeline_has_capacity", return_value=False),
        patch.object(pipeline_worker, "perform") as perform,
    ):
        pipeline_worker.advance(str(workspace.pk))
    perform.assert_not_called()
    run.refresh_from_db()
    assert run.status == "queued"
    assert run.progress == {}
    assert "staging space" in run.message
    run.status = "running"
    run.save()
    envelope = BackgroundJob.objects.get(
        key=f"apps.video_analysis.tasks.advance_pipeline:{workspace.pk}"
    )
    envelope.due_at = None
    envelope.error = "WorkerLost"
    envelope.save()
    assert pipeline.listing(workspace)["runs"][0]["status"] == "failed"
    pipeline.control(
        workspace, {"id": str(run.pk), "revision": run.revision, "action": "resume"}
    )
    run.refresh_from_db()
    assert run.status == "queued"
    envelope.refresh_from_db()
    assert envelope.due_at is not None


def test_correction_drafts_keep_resolved_clip_identity_and_team(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """Correcting replay does not begin from anonymous boxes or change raw evidence."""
    owner, store, workspace = prepared
    clip = make_clip(prepared)
    run = pipeline.submit(
        workspace,
        owner,
        store,
        {"request_id": str(uuid.uuid4()), "clip_review_id": clip.pk, "seconds": 0},
    )
    folder = store.root / "vision/clips" / str(clip.job_id)
    objects = [
        {
            "label": "player",
            "track_id": "temporary",
            "team": "unknown",
            "bbox": [0.1, 0.2, 0.1, 0.3],
        }
    ]
    original = {"frames": [{"time_seconds": 0, "objects": objects}]}
    atomic_json(folder / "chunk-00000.json", original)
    atomic_json(
        folder / "run.json",
        {
            "status": "completed",
            "chunks": [{"name": "chunk-00000.json", "start": 0, "end": 0}],
            "identity_refinement": {
                "status": "completed",
                "links": [],
                "frame_links": [
                    {
                        "time_seconds": 0,
                        "from_track_id": "temporary",
                        "to_track_id": "established",
                        "team": "team_a",
                    }
                ],
            },
        },
    )
    drafts = clip_drafts(
        run, store, [{"id": "at-000000000", "frame_version": "unchanged"}]
    )
    obj = drafts[0]["prediction"]["objects"][0]
    assert obj["team"] == "team_a"
    assert obj["track_id"] == f"{clip.job_id}-established"
    assert objects[0]["track_id"] == "temporary"


def test_preparation_history_cursor_keeps_older_work_reachable(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """An intake awaiting cuts remains reachable beyond the first fifty rows."""
    owner, _, workspace = prepared
    rows = [
        ReviewPipeline.objects.create(
            workspace=workspace,
            requested_by=owner,
            status="awaiting_cuts",
            recipe={"match_id": f"source-{i}", "stage": "import"},
        )
        for i in range(52)
    ]
    first = pipeline.listing(workspace)
    assert len(first["runs"]) == pipeline.RUN_PAGE_SIZE
    second = pipeline.listing(workspace, before=first["next_runs"])
    assert {row["id"] for row in second["runs"]} == {str(row.pk) for row in rows[:2]}
    assert second["next_runs"] is None
    assert not {row["id"] for row in first["runs"]}.intersection(
        row["id"] for row in second["runs"]
    )


def test_preparation_resolves_the_import_cutting_queue(
    prepared: tuple[User, DatabaseStore, Workspace],
) -> None:
    """Submitting cuts retires the import action while retaining its provenance."""
    owner, store, workspace = prepared
    intake = ReviewPipeline.objects.create(
        workspace=workspace,
        requested_by=owner,
        status="awaiting_cuts",
        recipe={"match_id": "demo", "stage": "import"},
    )
    pipeline.submit(workspace, owner, store, request())
    intake.refresh_from_db()
    assert intake.status == "prepared"
    assert intake.revision == 1
