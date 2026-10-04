"""Long replays share one job and retain private completed progress on interruption."""

from collections.abc import Callable
from dataclasses import asdict
from http import HTTPStatus
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
import pytest

from apps.video_analysis.adapters.detector import run_with_progress
from apps.video_analysis.adapters.replay import ReplaySection
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.clip_closed_set import (
    Evidence,
    ReviewSession,
    Scope,
    Settings as Recipe,
)
from apps.video_analysis.engine.clip_contract import MAX_RUNTIME_SECONDS, ClipOptions
from apps.video_analysis.engine.clip_match_evidence import fit_fragments
from apps.video_analysis.engine.clip_number_anchors import KitRead
from apps.video_analysis.engine.clip_section_identity import (
    GALLERY_FILE,
    open_gallery,
    remember,
)
from apps.video_analysis.engine.clips import WORKER_PID, directory, launch, receipt
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.vision import digest
from apps.video_analysis.models import AnalysisJob, Recording
from apps.video_analysis.services.jobs import continue_analysis
from apps.video_analysis.tasks import execute
from apps.video_analysis.tests.test_clips import ready
from apps.video_analysis.tests.test_identity_review import FOUR, raw_view
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db
SOURCE_SECONDS = 250
SECTION_SECONDS = 120
EXPECTED_SECTIONS = 2
BOUNDARY_SECONDS = 50


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
        "possession": {
            "status": "candidate",
            "holder_candidate": {"track_id": "player-1"},
        },
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
        "team_resolution": {
            "version": 1,
            "spans": [
                {
                    "track_id": "player-1",
                    "team": "team_a",
                    "start": frame["time_seconds"],
                    "end": frame["time_seconds"],
                    "votes": 5,
                    "share": 1.0,
                }
            ],
            "truncated": False,
        },
        "identity_refinement": {
            "status": "completed",
            "frame_links": [
                {
                    "time_seconds": frame["time_seconds"],
                    "from_track_id": "temporary",
                    "superseded_track_id": "player-1",
                    "to_track_id": "player-1",
                    "display_id": 1,
                }
            ],
            "links": [
                {
                    "from_track_id": "player-2",
                    "to_track_id": "player-1",
                    "display_id": 1,
                }
            ],
        },
        "event_detection": {
            "version": 2,
            "processed_frames": 1,
            "review_only": True,
            "truncated": False,
        },
        "events": [
            {
                "id": "s0-shot-1",
                "segment": 0,
                "kind": "shot_candidate",
                "time_seconds": frame["time_seconds"],
                "outcome": "unknown",
                "ball": {"track_id": "ball-1"},
                "shooter_candidate": {"track_id": "player-1"},
                "team": "team_a",
            },
            {
                "id": "s0-possession-1",
                "segment": 0,
                "kind": "ball_recovery_candidate",
                "time_seconds": frame["time_seconds"],
                "shot_event_id": "s0-shot-1",
                "recovery": "offensive_rebound",
                "loss_candidate": None,
                "gain_candidate": {"track_id": "player-1"},
            },
        ],
        "possession_detection": {
            "version": 2,
            "processed_frames": 1,
            "review_only": True,
            "truncated": False,
        },
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
    assert callable(progress)
    progress(result)
    result["events"][0]["outcome"] = "possible_goal"
    atomic_json(root / "run.json", result)
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
    assert len(record["events"]) == EXPECTED_SECTIONS * 2
    assert record["possession_detection"]["processed_frames"] == EXPECTED_SECTIONS
    assert record["event_detection"]["processed_frames"] == EXPECTED_SECTIONS
    assert record["event_detection"]["section_boundaries"] is True
    assert [
        (span["track_id"], span["processing_section"])
        for span in record["team_resolution"]["spans"]
    ] == [(f"p{part}-player-1", part) for part in range(EXPECTED_SECTIONS)]
    assert record["identity_refinement"]["links"] == [
        {
            "from_track_id": f"p{part}-player-2",
            "to_track_id": f"p{part}-player-1",
            "display_id": 1,
            "processing_section": part,
        }
        for part in range(EXPECTED_SECTIONS)
    ]
    frame_links = record["identity_refinement"]["frame_links"]
    assert len(frame_links) == EXPECTED_SECTIONS
    for part, link in enumerate(frame_links):
        assert link["from_track_id"] == f"p{part}-temporary"
        assert link["to_track_id"] == f"p{part}-player-1"
        assert link["processing_section"] == part
        assert link["superseded_track_id"] == f"p{part}-player-1"
    for part, event in enumerate(
        e for e in record["events"] if e["kind"] == "shot_candidate"
    ):
        assert event["id"] == f"p{part}-s0-shot-1"
        assert event["outcome"] == "possible_goal"
        assert event["ball"]["track_id"] == f"p{part}-ball-1"
        assert event["shooter_candidate"]["track_id"] == f"p{part}-player-1"
        assert event["processing_section"] == part
    for part, event in enumerate(
        e for e in record["events"] if e["kind"] == "ball_recovery_candidate"
    ):
        assert event["id"] == f"p{part}-s0-possession-1"
        assert event["shot_event_id"] == f"p{part}-s0-shot-1"
        assert event["gain_candidate"]["track_id"] == f"p{part}-player-1"
        assert event["loss_candidate"] is None
    for part, chunk in enumerate(record["chunks"]):
        frame = json.loads(
            (directory(store, run_id) / chunk["name"]).read_text(encoding="utf-8")
        )["frames"][0]
        assert frame["objects"][0]["track_id"] == f"p{part}-player-1"
        assert frame["top_down"]["ball"]["near_player"] == f"p{part}-player-1"
        assert frame["processing_section"] == part
        assert (
            frame["possession"]["holder_candidate"]["track_id"] == f"p{part}-player-1"
        )


def test_a_section_out_of_budget_hands_over_at_its_first_unprocessed_frame(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The next section starts where the previous one stopped, not where planned."""
    _, store, _ = imported
    payload = {**ready(store), "recording_end": 250}
    run_id = payload.pop("request_id")

    def out_of_budget(
        store: Store, child: str, request: dict, **kwargs: Callable
    ) -> None:
        child_result(store, child, request, **kwargs)
        record = receipt(store, child)
        record["section_end_seconds"] = request["options"]["start"] + BOUNDARY_SECONDS
        atomic_json(directory(store, child) / "run.json", record)

    ReplaySection(store, run_id, payload).run(out_of_budget)
    analyze = Mock(side_effect=child_result)
    ReplaySection(store, run_id, payload).run(analyze)
    request = analyze.call_args.args[2]
    assert request["options"]["start"] == 10 + BOUNDARY_SECONDS
    # Server-owned context: the child shares this replay's identity gallery.
    assert request["section"] == {"part": 1, "parent": run_id}
    assert receipt(store, run_id)["budget_boundaries"] == 1


def test_a_budget_boundary_in_the_last_second_never_strands_the_replay(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A sub-second remainder is not queued as an unparseable section.

    Review v1 finding 8: a child stopping at 249.52 of a 250 s recording left a
    0.48 s part that ClipOptions rejects, so the replay failed. The engine no
    longer closes within the last second; an older receipt that did is
    finished with the unanalysed tail recorded.
    """
    _, store, _ = imported
    payload = {**ready(store), "recording_end": 250}
    payload["options"] = {**payload["options"], "start": 130}
    run_id = payload.pop("request_id")

    def late_boundary(
        store: Store, child: str, request: dict, **kwargs: Callable
    ) -> None:
        child_result(store, child, request, **kwargs)
        record = receipt(store, child)
        record["section_end_seconds"] = 249.52
        atomic_json(directory(store, child) / "run.json", record)

    ReplaySection(store, run_id, payload).run(late_boundary)
    manifest = receipt(store, run_id)
    assert manifest["status"] == "completed"
    assert manifest["unprocessed_tail_seconds"] == pytest.approx(0.48)
    assert manifest["next_start"] == pytest.approx(250)


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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death signal")
def test_a_section_subprocess_dies_with_its_worker() -> None:
    """A redelivered section restarts in place, so no orphan may keep writing.

    A hard time limit or the OOM killer can kill the worker process while its
    detector subprocess runs; the kernel must then kill the subprocess too. The
    child binds itself (``bind_to_worker``) from the worker's PID.
    """
    probe = (
        "import ctypes; from apps.video_analysis.engine.clips import bind_to_worker; "
        "bind_to_worker(); value = ctypes.c_int(); "
        "ctypes.CDLL(None).prctl(2, ctypes.byref(value)); print(value.value)"
    )
    environment = {**os.environ, WORKER_PID: str(os.getpid())}
    command = [sys.executable, "-c", probe]
    output = subprocess.run(
        command, env=environment, capture_output=True, text=True, check=True
    ).stdout
    assert int(output) == signal.SIGKILL
    gone = {**environment, WORKER_PID: "1"}
    orphan = subprocess.run(command, env=gone, capture_output=True, check=False)
    assert orphan.returncode != 0
    with patch("apps.video_analysis.adapters.detector.subprocess.Popen") as popen:
        popen.return_value.__enter__.return_value.poll.return_value = 0
        popen.return_value.__enter__.return_value.returncode = 0
        run_with_progress(["detector"], {}, Path("missing.json"), Mock())
    assert popen.call_args.kwargs["env"][WORKER_PID] == str(os.getpid())


def test_a_lost_active_section_restarts_as_a_recorded_attempt(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A section whose worker died (its child files lost too) starts again.

    It is never silently recreated: the attempt is counted in the receipt and
    the automatic restarts are bounded; an explicit retry is still possible.
    """
    _, store, _ = imported
    payload = {**ready(store), "recording_end": SOURCE_SECONDS}
    run_id = payload.pop("request_id")
    section = ReplaySection(store, run_id, payload)
    section.record["status"] = "running"
    section.save()
    analyze = Mock(side_effect=child_result)
    ReplaySection(store, run_id, payload).run(analyze)
    assert analyze.called
    record = receipt(store, run_id)
    assert (record["completed_parts"], record["section_attempts"]) == (1, {"0": 1})
    assert [c["name"] for c in record["chunks"]] == ["part-0000-r1-chunk-00000.json"]
    exhausted = ReplaySection(store, run_id, payload)
    exhausted.record.update(status="running", section_attempts={"1": 3})
    exhausted.save()
    never = Mock()
    with pytest.raises(ValueError, match="interrupted 3 times"):
        ReplaySection(store, run_id, payload).run(never)
    assert not never.called
    assert receipt(store, run_id)["status"] == "failed"
    ReplaySection(store, run_id, payload).run(
        Mock(side_effect=child_result), retry=True
    )
    assert receipt(store, run_id)["status"] == "completed"


def failing_child(
    store: Store, run_id: str, payload: dict, *, progress: Callable[[dict], None]
) -> None:
    """Publish part of a section and leave naming files, then fail transiently.

    Raises:
        RuntimeError: Always, after the partial publication.

    """
    root = directory(store, run_id)
    root.mkdir(parents=True, exist_ok=True)
    chunk = root / "chunk-00000.json"
    start = payload["options"]["start"]
    atomic_json(chunk, {"frames": [{"time_seconds": start, "segment": 0}]})
    progress({
        "status": "running",
        "frames": 1,
        "video_sha256": "frozen-source",
        "weights_sha256": digest(store.root / "vision/runs/model/fit/weights/best.pt"),
        "chunks": [{"name": chunk.name, "sha256": digest(chunk), "frames": 1}],
        "event_detection": {"version": 2, "processed_frames": 1},
        "events": [{"id": "partial", "segment": 0, "time_seconds": start}],
    })
    # A section can fail after it named players: its cache and evidence exist.
    (root / "identity-review.sqlite").write_bytes(b"stale attempt")
    (root / "identity-evidence.json").write_text("{}")
    atomic_json(root / "run.json", {"status": "running", "chunks": []})
    msg = "transient worker failure"
    raise RuntimeError(msg)


def fresh_child(
    store: Store, run_id: str, payload: dict, *, progress: Callable[[dict], None]
) -> None:
    """Complete a section, first checking nothing of a failed attempt is reused."""
    root = directory(store, run_id)
    assert not (root / "identity-review.sqlite").exists()
    assert not (root / "identity-evidence.json").exists()
    assert not (root / "run.json").exists()
    child_result(store, run_id, payload, progress=progress)


def test_a_failed_section_is_retried_alone_keeping_committed_sections(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Review round 3, finding 4: a transient failure costs one section, not all.

    Section 0 is committed; section 1 publishes a partial chunk and fails. A
    redelivery still runs nothing, but the server-owned retry resumes at
    section 1: the failed attempt's private files are discarded, its partial
    progress leaves the replay, its published chunk is never rewritten, and
    the new attempt publishes under new names without double counts.
    """
    _, store, _ = imported
    payload = {**ready(store), "recording_end": SOURCE_SECONDS}
    run_id = payload.pop("request_id")
    ReplaySection(store, run_id, payload).run(child_result)
    with pytest.raises(RuntimeError):
        ReplaySection(store, run_id, payload).run(failing_child)
    failed = receipt(store, run_id)
    assert failed["status"] == "failed"
    partial = directory(store, run_id) / "part-0001-chunk-00000.json"
    published = partial.read_bytes()
    redelivered = Mock(side_effect=child_result)
    ReplaySection(store, run_id, payload).run(redelivered)
    assert not redelivered.called
    ReplaySection(store, run_id, payload).run(Mock(side_effect=fresh_child), retry=True)
    record = receipt(store, run_id)
    assert (record["status"], record["completed_parts"]) == ("completed", 2)
    assert [c["name"] for c in record["chunks"]] == [
        "part-0000-chunk-00000.json",
        "part-0001-r1-chunk-00000.json",
    ]
    assert record["chunks"][0] == failed["chunks"][0]
    assert record["frames"] == EXPECTED_SECTIONS
    assert record["section_attempts"] == {"1": 1}
    assert [e["id"] for e in record["events"]] == [
        "p0-s0-shot-1",
        "p0-s0-possession-1",
        "p1-s0-shot-1",
        "p1-s0-possession-1",
    ]
    assert partial.read_bytes() == published


def test_the_engine_starts_the_restarted_attempt_of_an_interrupted_section(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The real ``launch`` refused an interrupted child; a restart can run.

    The worker died mid-section: the parent says running and the child's
    marker says it started. The engine's own fence ("this attempt already
    started") stays, so the replay discards that attempt first.
    """
    _, store, _ = imported
    payload = {**ready(store), "recording_end": SOURCE_SECONDS}
    run_id = payload.pop("request_id")
    section = ReplaySection(store, run_id, payload)
    section.record["status"] = "running"
    section.save()
    options = ClipOptions.parse(section.child_payload["options"])
    weights = store.root / "vision/runs/model/fit/weights/best.pt"
    section.child_root.mkdir(parents=True)
    atomic_json(
        section.child_root / "run.json",
        {
            "status": "running",
            "chunks": [],
            "recipe": {
                "match_id": "demo",
                "options": asdict(options),
                "weights_sha256": digest(weights),
            },
        },
    )
    match = {**store.recording("demo"), "duration_seconds": SOURCE_SECONDS}
    request = {"weights": str(weights), "section": section.child_payload["section"]}
    with pytest.raises(ValueError, match="already started"):
        launch(store, section.child_id, {**request, "match": match, "options": options})

    class Run:
        """The engine's run object without a detector (OpenCV is not here)."""

        def __init__(self, root: Path, *_: object) -> None:
            self.root, self.record = root, {"status": "running", "chunks": []}

        def execute(self, *_: object) -> None:
            self.record.update(status="completed", message="Done", frames=1)
            atomic_json(self.root / "run.json", self.record)

    def engine(store: Store, run_id: str, child: dict, **_: object) -> None:
        launch(
            store,
            run_id,
            {**request, "match": match, "options": ClipOptions.parse(child["options"])},
        )

    with patch("apps.video_analysis.engine.clips.ClipRun", Run):
        ReplaySection(store, run_id, payload).run(engine)
    record = receipt(store, run_id)
    assert (record["completed_parts"], record["section_attempts"]) == (1, {"0": 1})


def test_a_retried_section_withdraws_its_gallery_commit(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The failed attempt's named views and kit sightings leave the gallery.

    A section can fail after it committed to the replay's identity gallery.
    Its attempt is discarded, so its views (from tracklets that no longer
    exist) and its kit-orientation sightings must not count next to the new
    attempt's; the committed section 0 keeps everything.
    """
    _, store, _ = imported
    payload = {**ready(store), "recording_end": SOURCE_SECONDS}
    run_id = payload.pop("request_id")
    ReplaySection(store, run_id, payload).run(child_result)
    path = directory(store, run_id) / GALLERY_FILE
    gallery = open_gallery(path, "demo", "synthetic-v1")
    assert gallery is not None
    for part in (0, 1):
        pieces = [raw_view(f"v{part}{i}", part * 200 + i, i) for i in range(4)]
        fit_fragments(pieces, dimensions=8)
        session = ReviewSession(
            Scope("demo", f"s{part}"),
            pieces,
            FOUR,
            Recipe("synthetic-v1"),
            Evidence(
                numbers=(
                    KitRead(f"v{part}0", "team_a", "7", 0.9),
                    KitRead(f"v{part}2", "team_a", "4", 0.9),
                ),
                section=f"part-{part:04d}",
            ),
        )
        span = {
            "id": f"part-{part:04d}",
            "start": part * 200.0,
            "end": part * 200.0 + 100,
            "fingerprint": f"f{part}",
        }
        remember(gallery, span, pieces, session.result, FOUR, session.sightings())

    def committed() -> dict[str, set[str]]:
        with sqlite3.connect(path) as db:
            return {
                "views": {
                    r[0].split(":")[0] for r in db.execute("SELECT seed FROM samples")
                },
                "sightings": {
                    r[0] for r in db.execute("SELECT section FROM sightings")
                },
                "sections": {r[0] for r in db.execute("SELECT id FROM sections")},
            }

    assert committed()["sightings"] == {"part-0000", "part-0001"}
    with pytest.raises(RuntimeError):
        ReplaySection(store, run_id, payload).run(failing_child)

    def checked(*args: object, **kwargs: object) -> None:
        assert committed() == {
            "views": {"part-0000"},
            "sightings": {"part-0000"},
            "sections": {"part-0000"},
        }
        child_result(*args, **kwargs)  # type: ignore[arg-type]

    ReplaySection(store, run_id, payload).run(Mock(side_effect=checked), retry=True)
    assert receipt(store, run_id)["status"] == "completed"


ROSTER = [{"player_id": "alice", "team": "team_a", "number": "7"}]


def tracklet_child(
    store: Store, run_id: str, payload: dict, *, progress: Callable[[dict], None]
) -> None:
    """Complete a section whose one pure tracklet the match pass can name."""
    child_result(store, run_id, payload, progress=progress)
    record = receipt(store, run_id)
    record["identity_refinement"]["frame_links"].append({
        "time_seconds": payload["options"]["start"],
        "from_track_id": "player-3",
        "to_track_id": "s0-linked-3",
        "display_id": 3,
        "fragment_identity": "tracklet:3",
        "unnamed_track_id": "s0-linked-3",
        "unnamed_display_id": 3,
    })
    atomic_json(directory(store, run_id) / "run.json", record)
    progress(record)


def test_a_finished_replay_names_players_across_all_sections(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """After the last section, one match pass renames every section's links."""
    _, store, _ = imported
    payload = {**ready(store), "recording_end": 250}
    payload["options"] = {
        **payload["options"],
        "match_identity": {"version": 1, "closed_set": {"roster": ROSTER}},
    }
    run_id = payload.pop("request_id")
    seen: list[int] = []

    def match(store: Store, replay: str) -> None:
        seen.append(receipt(store, replay)["completed_parts"])
        atomic_json(
            directory(store, replay) / "match-identity.json",
            {
                "status": "completed",
                "sections": EXPECTED_SECTIONS,
                "tracklets": EXPECTED_SECTIONS,
                "number_anchors": {"accepted": 1},
                "assignments": [
                    {
                        "identity": "p0001:tracklet:3",
                        "player_id": "alice",
                        "display_id": "#7",
                        "origin": "automatic",
                        "source": "shirt_number",
                    },
                    {"identity": "p0000:tracklet:3", "player_id": None},
                ],
            },
        )

    for _ in range(3):
        ReplaySection(store, run_id, payload).run(
            Mock(side_effect=tracklet_child), match=match
        )
    record = receipt(store, run_id)
    assert record["status"] == "completed"
    assert seen == [EXPECTED_SECTIONS]
    named = {
        link["from_track_id"]: link
        for link in record["identity_refinement"]["frame_links"]
        if link.get("fragment_identity")
    }
    assert named["p1-player-3"]["to_track_id"] == "alice"
    assert named["p1-player-3"]["name_source"] == "shirt_number"
    assert named["p0-player-3"]["to_track_id"] == "p0-s0-linked-3"
    assert record["match_identity_wide"]["status"] == "completed"


def test_a_failed_match_pass_keeps_the_section_names(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The replay completes with per-section names when the match pass fails."""
    _, store, _ = imported
    payload = {**ready(store), "recording_end": 250}
    payload["options"] = {
        **payload["options"],
        "match_identity": {"version": 1, "closed_set": {"roster": ROSTER}},
    }
    run_id = payload.pop("request_id")
    failing = Mock(side_effect=subprocess.CalledProcessError(1, "match"))
    for _ in range(EXPECTED_SECTIONS):
        ReplaySection(store, run_id, payload).run(
            Mock(side_effect=tracklet_child), match=failing
        )
    record = receipt(store, run_id)
    assert record["status"] == "completed"
    assert record["match_identity_wide"]["status"] == "failed"
    assert record["match_identity_wide"]["code"] == "match_pass_failed"
    assert {
        link["to_track_id"]
        for link in record["identity_refinement"]["frame_links"]
        if link.get("fragment_identity")
    } == {"p0-s0-linked-3", "p1-s0-linked-3"}
