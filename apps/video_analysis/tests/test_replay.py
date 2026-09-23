"""Long replays share one job and retain private completed progress on interruption."""

from collections.abc import Callable
from http import HTTPStatus
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
import pytest

from apps.video_analysis.adapters.detector import run_with_progress
from apps.video_analysis.adapters.replay import ReplaySection
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.clip_contract import MAX_RUNTIME_SECONDS
from apps.video_analysis.engine.clips import directory, receipt
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.vision import digest
from apps.video_analysis.models import AnalysisJob, Recording
from apps.video_analysis.services.jobs import continue_analysis
from apps.video_analysis.tasks import execute
from apps.video_analysis.tests.test_clips import ready
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db
SOURCE_SECONDS = 250
SECTION_SECONDS = 120
EXPECTED_SECTIONS = 2


def test_recording_scope_binds_server_duration_and_reuses_request(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A browser cannot extend the source duration or create duplicate replay jobs."""
    owner, store, _ = imported
    payload = {**ready(store), "scope": "recording", "recording_end": 999999}
    recording = Recording.objects.get(source_id="demo")
    recording.metadata["duration_seconds"] = 250
    recording.save()
    client = verified(owner)
    csrf = client.get("/video-analysis/clips").json()["csrf"]
    with patch("apps.video_analysis.services.jobs.enqueue") as enqueue:
        response = client.post(
            "/video-analysis/clips",
            payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        retry = client.post(
            "/video-analysis/clips",
            payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
    assert response.status_code == HTTPStatus.ACCEPTED
    assert retry.json() == response.json()
    assert enqueue.call_count == 1
    job = AnalysisJob.objects.get()
    assert job.payload["recording_end"] == SOURCE_SECONDS
    assert job.payload["options"]["duration"] == SECTION_SECONDS


@pytest.mark.parametrize("duration", [None, 14401, "Infinity"])
def test_recording_scope_requires_a_bounded_duration(
    imported: tuple[User, DatabaseStore, Store], duration: float | str | None
) -> None:
    """Unknown or excessive source lengths cannot schedule unbounded CPU work."""
    owner, store, _ = imported
    payload = {**ready(store), "scope": "recording"}
    recording = Recording.objects.get(source_id="demo")
    recording.metadata["duration_seconds"] = duration
    recording.save()
    client = verified(owner)
    csrf = client.get("/video-analysis/clips").json()["csrf"]
    response = client.post(
        "/video-analysis/clips",
        payload,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert not AnalysisJob.objects.exists()


def child_result(
    store: Store, run_id: str, payload: dict, *, progress: Callable[[dict], None]
) -> None:
    """Emit one deterministic completed section without importing any model runtime."""
    root = directory(store, run_id)
    chunk = root / "chunk-00000.json"
    frame = {
        "time_seconds": payload["options"]["start"],
        "segment": 0,
        "objects": [{"track_id": "player-1"}],
        "top_down": {"ball": {"near_player": "player-1"}},
    }
    atomic_json(chunk, {"frames": [frame]})
    result = {
        "status": "completed",
        "message": "Done",
        "frames": 1,
        "runtime_seconds": 2,
        "weights_sha256": digest(store.root / "vision/runs/model/fit/weights/best.pt"),
        "video_sha256": "frozen-source",
        "team_colors": [[20, 40, 150], [30, 130, 60]],
        "video": "demo/synthetic.mp4",
        "chunks": [
            {
                "name": chunk.name,
                "sha256": digest(chunk),
                "start": frame["time_seconds"],
                "end": frame["time_seconds"],
                "frames": 1,
            }
        ],
    }
    atomic_json(root / "run.json", result)
    assert callable(progress)
    progress(result)
    progress(
        result
    )  # A poll and the final receipt must not duplicate counts or chunks.


def test_sections_publish_one_replay_without_duplicate_counts_or_track_ids(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Repeated delivery resumes at the committed boundary; terminal replay is inert."""
    _, store, _ = imported
    payload = {**ready(store), "recording_end": 250}
    run_id = payload.pop("request_id")
    analyze = Mock(side_effect=child_result)
    for _ in range(3):
        ReplaySection(store, run_id, payload).run(analyze)
    record = receipt(store, run_id)
    assert record["status"] == "completed"
    assert record["frames"] == EXPECTED_SECTIONS
    assert record["runtime_seconds"] == EXPECTED_SECTIONS * 2
    assert record["recipe"]["options"]["duration"] == SOURCE_SECONDS - 10
    assert analyze.call_count == EXPECTED_SECTIONS
    assert [call.args[2]["options"]["start"] for call in analyze.call_args_list] == [
        10,
        130,
    ]
    assert analyze.call_args_list[1].args[2]["options"]["team_colors"] == [
        [20, 40, 150],
        [30, 130, 60],
    ]
    assert len(record["chunks"]) == EXPECTED_SECTIONS
    for part, chunk in enumerate(record["chunks"]):
        frame = json.loads(
            (directory(store, run_id) / chunk["name"]).read_text(encoding="utf-8")
        )["frames"][0]
        assert frame["objects"][0]["track_id"] == f"p{part}-player-1"
        assert frame["top_down"]["ball"]["near_player"] == f"p{part}-player-1"
        assert frame["processing_section"] == part


@pytest.mark.parametrize("stop", ["cancel", "checkpoint", "source", "worker"])
def test_interrupted_replay_retains_prior_sections(
    imported: tuple[User, DatabaseStore, Store], stop: str
) -> None:
    """Stop before/between sections and input changes cannot silently restart work."""
    _, store, _ = imported
    payload = {**ready(store), "recording_end": 250}
    run_id = payload.pop("request_id")
    ReplaySection(store, run_id, payload).run(child_result)
    first = receipt(store, run_id)
    section = ReplaySection(store, run_id, payload)
    analyze = Mock(side_effect=RuntimeError("worker failed"))
    if stop == "cancel":
        atomic_json(section.root / "cancel.json", {"requested": True})
        section.run(analyze)
        assert not analyze.called
    else:
        if stop == "checkpoint":
            (store.root / "vision/runs/model/fit/weights/best.pt").write_bytes(
                b"changed"
            )
        elif stop == "source":
            analyze.side_effect = lambda *args, **kwargs: kwargs["progress"]({
                "video_sha256": "different"
            })
        with pytest.raises((ValueError, RuntimeError)):
            section.run(analyze)
    result = receipt(store, run_id)
    assert result["status"] == ("cancelled" if stop == "cancel" else "failed")
    assert result["chunks"] == first["chunks"]
    assert result["frames"] == first["frames"]
    assert "finished_at" in result


def test_midsection_cancellation_reaches_the_child(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A stop while inference is publishing is forwarded before accepting completion."""
    _, store, _ = imported
    payload = {**ready(store), "recording_end": 250}
    run_id = payload.pop("request_id")
    section = ReplaySection(store, run_id, payload)

    def analyze(*args: object, **kwargs: object) -> None:
        atomic_json(section.root / "cancel.json", {"requested": True})
        child_result(*args, **kwargs)
        assert (section.child_root / "cancel.json").is_file()

    section.run(analyze)
    assert receipt(store, run_id)["status"] == "cancelled"
    assert receipt(store, run_id)["frames"] == 1


def test_task_advances_same_durable_generation_without_finishing(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A continuation is published through the durable runner after its lease ends."""
    owner, store, _ = imported
    payload = {**ready(store), "recording_end": 250}
    job = AnalysisJob.objects.create(
        workspace_id=store.workspace_id,
        requested_by=owner,
        kind="clip",
        payload=payload,
    )
    with (
        patch("apps.video_analysis.tasks.worker_store", return_value=store),
        patch("apps.video_analysis.tasks.run_clip"),
        patch(
            "apps.video_analysis.tasks.receipt",
            return_value={"status": "queued", "message": "Next"},
        ),
        patch("apps.video_analysis.services.jobs.enqueue") as enqueue,
    ):
        execute(str(job.pk))
    job.refresh_from_db()
    assert job.status == "queued"
    assert job.finished_at is None
    enqueue.assert_called_once_with(
        "apps.video_analysis.tasks.execute",
        str(job.pk),
        args=[str(job.pk)],
        queue="vision",
    )
    job.status = "cancelled"
    job.save()
    with patch("apps.video_analysis.services.jobs.enqueue") as enqueue:
        continue_analysis(str(job.pk))
    assert not enqueue.called


@pytest.mark.parametrize("failure", ["deadline", "publication"])
def test_progress_runner_kills_only_its_owned_child_on_failure(
    tmp_path: Path, failure: str
) -> None:
    """A hung detector or failed upload cannot leave inference running unattended."""
    marker = tmp_path / "run.json"
    atomic_json(marker, {"status": "running", "chunks": []})
    child = Mock()
    child.poll.return_value = None
    if failure == "publication":
        child.wait.side_effect = [subprocess.TimeoutExpired(["detector"], 5), 0]
    else:
        child.wait.return_value = 0
    process = Mock()
    process.__enter__ = Mock(return_value=child)
    process.__exit__ = Mock(return_value=False)
    progress = Mock(side_effect=ValueError("publication failed"))
    times = [0, MAX_RUNTIME_SECONDS + 61] if failure == "deadline" else [0, 1]
    with (
        patch(
            "apps.video_analysis.adapters.detector.subprocess.Popen",
            return_value=process,
        ),
        patch(
            "apps.video_analysis.adapters.detector.time.monotonic", side_effect=times
        ),
        pytest.raises(
            ValueError if failure == "publication" else subprocess.TimeoutExpired
        ),
    ):
        run_with_progress(["detector"], {}, marker, progress)
    child.kill.assert_called_once_with()


def test_lost_active_section_cannot_restart_after_cache_loss(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """An incomplete cached child is never silently recreated from a parent receipt."""
    _, store, _ = imported
    payload = {**ready(store), "recording_end": SOURCE_SECONDS}
    run_id = payload.pop("request_id")
    section = ReplaySection(store, run_id, payload)
    section.record["status"] = "running"
    section.save()
    analyze = Mock()
    with pytest.raises(ValueError, match="interrupted section"):
        ReplaySection(store, run_id, payload).run(analyze)
    assert not analyze.called
    assert receipt(store, run_id)["status"] == "failed"
