"""A replay's match pass in production: staged inputs and published names.

These tests drive the real product path (``composition.run_clip`` -> replay
section -> match pass) with object storage enabled. Only the detector child
and the vision-runtime subprocess are replaced: the child writes synthetic
section evidence, and the match-pass command runs the real engine in-process.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from http import HTTPStatus
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, patch
import uuid

from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone
import pytest
from pytest_django.fixtures import Settings

from apps.kwt_common.models import BackgroundJob
from apps.video_analysis import tasks
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.application.ports import IdentityReviewRuntime
from apps.video_analysis.composition import processing_store, run_clip
from apps.video_analysis.engine import clip_identity_review, clip_match_wide
from apps.video_analysis.engine.clip_appearance_giant import FINGERPRINT
from apps.video_analysis.engine.clip_closed_set import (
    RECIPE,
    Evidence,
    Player,
    ReviewSession,
    Scope,
    Settings as Recipe,
)
from apps.video_analysis.engine.clip_closed_set_calibration import calibrated
from apps.video_analysis.engine.clip_identity_review import ReviewCache
from apps.video_analysis.engine.clip_match_evidence import fit_fragments
from apps.video_analysis.engine.clip_match_identity import Fragment
from apps.video_analysis.engine.clip_number_anchors import KitRead
from apps.video_analysis.engine.clips import directory, receipt
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.vision import digest
from apps.video_analysis.models import (
    AnalysisJob,
    ClipIdentityReview,
    Recording,
    StoredFile,
    Workspace,
)
from apps.video_analysis.services import clips, identity_review
from apps.video_analysis.tests.test_clips import ready
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db
np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

DIMENSIONS = 16
RECORDING_END = 250
SECTIONS = 2
ROSTER = [
    {"player_id": "alice", "team": "team_a", "number": "7"},
    {"player_id": "bob", "team": "team_a", "number": "9"},
    {"player_id": "carol", "team": "team_b", "number": "4"},
    {"player_id": "dave", "team": "team_b", "number": "5"},
]
POLICY = {
    "manifest_sha256": "fixture",
    "roster_anchors": {"approved": True, "threshold": 0.67, "min_support": 2},
}
ENGINE = "apps.video_analysis.engine.clip_match_wide"


def space(store: DatabaseStore) -> Workspace:
    """Load the store's workspace row."""
    return Workspace.objects.get(pk=store.workspace_id)


def replay_job(store: DatabaseStore, owner: User, roster: list | None = None) -> str:
    """Create a whole-recording clip job with a match roster.

    Returns:
        The replay's run ID.

    """
    payload = ready(store)
    recording = Recording.objects.get(source_id="demo")
    recording.metadata["duration_seconds"] = RECORDING_END
    recording.save()
    run_id = payload.pop("request_id")
    payload["recording_end"] = RECORDING_END
    payload["options"] = {
        "start": 10,
        "duration": 120,
        "match_identity": {
            "version": 1,
            "closed_set": {
                "input": "tracklets",
                "roster": ROSTER if roster is None else roster,
            },
        },
    }
    AnalysisJob.objects.create(
        id=run_id,
        workspace=space(store),
        requested_by=owner,
        kind="clip",
        status="running",
        payload=payload,
    )
    return run_id


def tracklet(identity: str, who: int, start: float, seed: int, team: str) -> Fragment:
    """Build a pure tracklet: 12 raw samples around one person's prototype."""
    rng = np.random.default_rng(seed)
    centre = np.zeros(DIMENSIONS)
    centre[who] = 1.0
    raw = centre + 0.05 * rng.standard_normal((12, DIMENSIONS))
    return Fragment(
        identity,
        "0",
        team,
        {round(start + i * 0.08, 6) for i in range(25)},
        raw={identity: raw.astype(np.float16)},
        weight=25,
    )


def evidence_child(
    reads: dict[int, dict[int, str]], fail: set[int] | None = None
) -> Callable:
    """Replace the detector child: write one section's chunk, links and evidence.

    Every roster player shows once per section (``tracklet:<roster index>``);
    ``reads[part]`` maps a tracklet to the shirt number read on it. Like a clip
    run with a roster, the section also leaves its own review cache. The first
    attempt of a part in ``fail`` publishes a partial chunk, leaves its naming
    files and then the worker fails.

    Returns:
        A stand-in for ``detector.run_with_progress``.

    """

    def child(
        command: list[str],
        environment: dict,
        marker: Path,
        progress: Callable[[dict], None],
    ) -> None:
        del environment
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        part = request["section"]["part"]
        start = request["options"]["start"]
        root = marker.parent
        root.mkdir(parents=True, exist_ok=True)
        pieces, numbers = [], {}
        for index, player in enumerate(ROSTER):
            identity = f"tracklet:{index}"
            pieces.append(
                tracklet(identity, index, start, 10 * part + index, player["team"])
            )
            if index in reads.get(part, {}):
                numbers[identity] = [{reads[part][index]: 0.95}] * 3
        evidence = clip_match_wide.save(
            root,
            {"id": f"part-{part:04d}", "start": start, "end": start + 2},
            pieces,
            numbers,
            {
                "descriptor_sha256": FINGERPRINT,
                "numbers": POLICY,
                "team_colors": None,
                "fixed_camera": False,
            },
        )
        chunk = root / "chunk-00000.json"
        atomic_json(
            chunk,
            {"frames": [{"time_seconds": start, "segment": 0, "objects": []}]},
        )
        links = [
            {
                "time_seconds": start,
                "from_track_id": f"raw-{index}",
                "to_track_id": f"linked-{index}",
                "display_id": index,
                "source": "closed_set_tracklet",
                "fragment_identity": f"tracklet:{index}",
                "unnamed_track_id": f"linked-{index}",
                "unnamed_display_id": index,
            }
            for index in range(len(ROSTER))
        ]
        section_review(
            root,
            pieces,
            numbers,
            links,
            request["options"]["match_identity"]["closed_set"]["roster"],
        )
        if fail is not None and part in fail:
            fail.discard(part)
            partial = {
                "status": "running",
                "frames": 1,
                "weights_sha256": digest(Path(request["weights"])),
                "video_sha256": "frozen-source",
                "chunks": [{"name": chunk.name, "sha256": digest(chunk), "frames": 1}],
            }
            atomic_json(marker, partial)
            progress(partial)
            raise subprocess.CalledProcessError(1, command)
        record = {
            "status": "completed",
            "message": "Done",
            "frames": 1,
            "weights_sha256": digest(Path(request["weights"])),
            "video_sha256": "frozen-source",
            "team_colors": [[20, 40, 150], [30, 130, 60]],
            "match_evidence": evidence,
            "identity_refinement": {
                "status": "completed",
                "frame_links": links,
                "links": [],
            },
            "chunks": [
                {
                    "name": chunk.name,
                    "sha256": digest(chunk),
                    "start": start,
                    "end": start,
                    "frames": 1,
                }
            ],
        }
        atomic_json(marker, record)
        progress(record)

    return child


def section_review(
    root: Path,
    pieces: list[Fragment],
    numbers: dict[str, list],
    links: list[dict],
    roster: list[dict],
) -> None:
    """Leave a section's naming cache, as ``ClipRun`` does with a roster."""
    fitted = deepcopy(pieces)
    fit_fragments(fitted, dimensions=8)
    kits = {piece.identity: piece.team for piece in pieces}
    scope = Scope("demo", root.name)
    ReviewCache(root / clip_match_wide.REVIEW, scope).create(
        ReviewSession(
            scope,
            fitted,
            [Player(**p) for p in roster],
            Recipe(
                f"{FINGERPRINT}:{RECIPE}:tracklets",
                calibrated(FINGERPRINT, "tracklets"),
            ),
            Evidence(
                numbers=tuple(
                    KitRead(identity, kits[identity], next(iter(read[0])), 0.95)
                    for identity, read in numbers.items()
                )
            ),
        ),
        refinement={"status": "completed", "frame_links": links, "links": []},
    )


def engine(command: list[str], **_: object) -> None:
    """Run the match-pass command in-process, as the vision runtime would.

    Raises:
        CalledProcessError: The engine failed, as the subprocess would report.

    """
    argv = ["clip_match_wide", *command[command.index(ENGINE) + 1 :], "--workers", "1"]
    with patch.object(sys, "argv", argv):
        try:
            clip_match_wide.main()
        except (OSError, ValueError, KeyError, SystemExit) as error:
            raise subprocess.CalledProcessError(1, command) from error


@contextmanager
def production(
    s3: MagicMock, reads: dict[int, dict[int, str]], fail: set[int] | None = None
) -> Iterator[MagicMock]:
    """Enable object storage with synthetic sections.

    Yields:
        The mock of the match-pass command (the real engine runs inside it).

    """

    @contextmanager
    def no_video(*_: object) -> Iterator[str]:
        yield "unused-by-a-synthetic-section"

    command = MagicMock(side_effect=engine)
    with (
        patch("apps.video_analysis.adapters.objects.object_client", return_value=s3),
        patch(
            "apps.video_analysis.adapters.objects.WorkspaceObjects.video_source",
            no_video,
        ),
        patch(
            "apps.video_analysis.adapters.detector.run_with_progress",
            side_effect=evidence_child(reads, fail),
        ),
        patch("apps.video_analysis.adapters.detector.subprocess.run", command),
    ):
        yield command


def run_section(store: DatabaseStore, run_id: str) -> None:
    """Run the next replay section as one vision job (own storage lease)."""
    job = AnalysisJob.objects.get(pk=run_id)
    with processing_store(space(store), job.requested_by) as worker:
        run_clip(worker, run_id, job.payload)


def without_crops(store: Store, run_id: str, request: dict) -> dict:
    """Run the real review command in-process, as the vision worker would."""
    output = store.root / "identity-output.json"
    command = store.root / "identity-request.json"
    atomic_json(
        command,
        {
            **request,
            "crops": None,
            "run_directory": str(directory(store, run_id)),
            "output": str(output),
        },
    )
    with patch.object(sys, "argv", ["review", str(command)]):
        clip_identity_review.main()
    return json.loads(output.read_text(encoding="utf-8"))


REVIEWS = IdentityReviewRuntime(processing_store=processing_store, solve=without_crops)


def answer(store: DatabaseStore, run_id: str, part: int, fields: dict) -> dict:
    """Answer through the naming endpoints' service and apply it on the worker.

    The review worker runs in its own lease: the cache is published and
    evicted when it ends.

    Returns:
        The section's review state after the answer.

    """
    section = f"{run_id}-part-{part:04d}"
    job = AnalysisJob.objects.get(pk=run_id)
    state = identity_review.read(store, space(store), section)
    with patch("apps.video_analysis.services.identity_review.enqueue"):
        if state["status"] == "unprepared":
            identity_review.prepare(store, space(store), section)
            identity_review.process(section, REVIEWS)
            state = identity_review.read(store, space(store), section)
        identity_review.submit(
            space(store),
            job.requested_by,
            {
                "run_id": section,
                "request_id": str(uuid.uuid4()),
                "expected_revision": state["expected_revision"],
                **fields,
            },
        )
    identity_review.process(section, REVIEWS)
    return identity_review.read(store, space(store), section)


def cold_worker(store: DatabaseStore, run_id: str) -> None:
    """Lose the worker's local disk: only object storage keeps the sections."""
    for part in range(SECTIONS):
        root = directory(store, f"{run_id}-part-{part:04d}")
        for path in root.glob("*"):
            path.unlink()


def published(store: DatabaseStore, run_id: str) -> dict[str, str]:
    """Return the replay's published name per section-scoped raw track."""
    record = receipt(store, run_id)
    return {
        link["from_track_id"]: link["to_track_id"]
        for link in record["identity_refinement"]["frame_links"]
        if link.get("fragment_identity")
    }


DAVE = {"identity": "tracklet:3", "player_id": "dave"}
EVERYONE_BUT_DAVE = {0: "7", 1: "9", 2: "4"}


@pytest.mark.parametrize("cold", [False, True])
def test_the_match_pass_restores_evicted_evidence_and_answers(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    s3: MagicMock,
    cold: bool,
) -> None:
    """Review round 3, finding 1: evicted section inputs are restored first.

    Section 0's evidence and its reviewer's answers (Dave confirmed) are
    published and evicted between jobs; on a cold worker even its JSON is gone.
    The match pass at the end of section 1 must restore both and solve both
    sections, so section 1's Dave is named from section 0's confirmation.
    """
    owner, store, _ = imported
    run_id = replay_job(store, owner)
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    with production(s3, {1: EVERYONE_BUT_DAVE}):
        run_section(store, run_id)
        assert answer(store, run_id, 0, DAVE)["revision"] == 1
        evidence = directory(store, f"{run_id}-part-0000")
        assert not (evidence / clip_match_wide.EVIDENCE).exists()
        assert not (evidence / clip_match_wide.REVIEW).exists()
        if cold:
            cold_worker(store, run_id)
        run_section(store, run_id)
    record = receipt(store, run_id)
    assert record["status"] == "completed"
    wide = record["match_identity_wide"]
    assert (wide["status"], wide["sections"], wide["confirmations"]) == (
        "completed",
        SECTIONS,
        1,
    )
    assert wide["section_reviews"] == {"part-0000": 1, "part-0001": 0}
    names = published(store, run_id)
    assert names["p0-raw-3"] == "dave"
    assert names["p1-raw-3"] == "dave"
    assert names["p1-raw-0"] == "alice"


@pytest.mark.parametrize("missing", ["evidence", "answers"])
def test_the_match_pass_never_runs_on_partial_inputs(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    s3: MagicMock,
    missing: str,
) -> None:
    """Unavailable evidence or unstaged answers fail the pass with a receipt.

    Section 0's evidence object is gone from storage, or the database applied a
    newer answer than the cache in storage holds. The engine must not run on
    what is left; the replay completes with section names and says why.
    """
    owner, store, _ = imported
    run_id = replay_job(store, owner)
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    with production(s3, {1: EVERYONE_BUT_DAVE}) as command:
        run_section(store, run_id)
        answer(store, run_id, 0, DAVE)
        if missing == "evidence":
            StoredFile.objects.filter(
                relative_path__endswith=f"{run_id}-part-0000/{clip_match_wide.EVIDENCE}"
            ).delete()
        else:
            ClipIdentityReview.objects.filter(
                job_id=run_id, section="part-0000"
            ).update(revision=2)
        run_section(store, run_id)
    assert not command.called
    record = receipt(store, run_id)
    assert record["status"] == "completed"
    wide = record["match_identity_wide"]
    assert (wide["status"], wide["code"]) == ("failed", "inputs_unavailable")
    assert "part-0000" in wide["message"]
    assert set(published(store, run_id).values()) == {
        f"p{part}-linked-{index}" for part in range(SECTIONS) for index in range(4)
    }


def queued_pass(run_id: str) -> BackgroundJob | None:
    """Return the replay's queued match pass, if any."""
    return BackgroundJob.objects.filter(
        key=f"apps.video_analysis.tasks.republish_match_identity:{run_id}"
    ).first()


def wrong_read_replay(
    imported: tuple[User, DatabaseStore, Store], s3: MagicMock
) -> tuple[DatabaseStore, str, MagicMock]:
    """Finish a replay whose match pass names Alice's body in section 1 as Bob.

    Section 0 reads every number correctly; in section 1 Alice's body reads
    as #9, Bob's number.

    Returns:
        The store, the replay ID and the match-pass command mock.

    """
    owner, store, _ = imported
    run_id = replay_job(store, owner)
    reads = {0: {0: "7", 1: "9", 2: "4", 3: "5"}, 1: {0: "9"}}
    with production(s3, reads) as command:
        run_section(store, run_id)
        run_section(store, run_id)
    AnalysisJob.objects.filter(pk=run_id).update(status="completed")
    assert published(store, run_id)["p1-raw-0"] == "bob"
    return store, run_id, command


def test_a_correction_republishes_the_match_wide_names(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    s3: MagicMock,
) -> None:
    """Review round 3, finding 2: an applied answer reaches the published replay.

    The reviewer names section 1's misread body Alice. The answer is applied
    to the section's cache, but the replay's published aliases (what playback
    shows without the naming panel, and every other section's match-wide
    names) still said Bob, and nothing re-ran the match pass. Now the answer
    queues one debounced pass on the vision worker, which republishes the
    replay with Alice; until then the replay says its names are stale.
    """
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    store, run_id, _ = wrong_read_replay(imported, s3)
    alice = {"identity": "tracklet:0", "player_id": "alice"}
    with production(s3, {}) as command:
        before = timezone.now()
        assert answer(store, run_id, 1, alice)["revision"] == 1
        job = queued_pass(run_id)
        assert job is not None
        assert job.queue == "vision"
        assert job.due_at >= before + timedelta(seconds=25)
        result = clips.result(store, space(store), run_id, None)
        assert result["match_identity_wide"]["stale"] is True
        assert published(store, run_id)["p1-raw-0"] == "bob"
        tasks.republish_match_identity(run_id)
        assert command.call_count == 1
    names = published(store, run_id)
    assert names["p1-raw-0"] == "alice"
    origins = {
        link["from_track_id"]: link.get("name_origin")
        for link in receipt(store, run_id)["identity_refinement"]["frame_links"]
    }
    assert origins["p1-raw-0"] == "human"
    assert names["p0-raw-0"] == "alice"
    wide = clips.result(store, space(store), run_id, None)["match_identity_wide"]
    assert (wide["status"], wide["stale"]) == ("completed", False)
    assert wide["section_reviews"]["part-0001"] == 1


def test_ten_quick_answers_queue_one_pass_and_a_current_replay_is_left_alone(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    s3: MagicMock,
) -> None:
    """Answers coalesce into one queued pass; a pass with nothing new is a no-op."""
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    store, run_id, _ = wrong_read_replay(imported, s3)
    with production(s3, {}) as command:
        for index in range(10):
            fields = (
                {"identity": "tracklet:0", "player_id": "alice"}
                if index % 2 == 0
                else {"action": "remove", "identity": "tracklet:0"}
            )
            answer(store, run_id, 1, fields)
        assert (
            BackgroundJob.objects.filter(
                task="apps.video_analysis.tasks.republish_match_identity"
            ).count()
            == 1
        )
        tasks.republish_match_identity(run_id)
        tasks.republish_match_identity(run_id)
        assert command.call_count == 1
    # The last answer undid the confirmation: Bob's misread stands again.
    assert published(store, run_id)["p1-raw-0"] == "bob"


@pytest.mark.parametrize("entry", ["engine", "management"])
def test_the_documented_manual_rerun_publishes(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    s3: MagicMock,
    entry: str,
) -> None:
    """Rerunning the match pass by hand renames the published replay as well.

    The engine command used to write only ``match-identity.json``; the replay's
    links kept the old names. The engine command renames a local store's
    replay, and the management command does the production path (staging,
    the worker's lease, publication).
    """
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = entry == "management"
    store, run_id, _ = wrong_read_replay(imported, s3)
    with production(s3, {}):
        answer(store, run_id, 1, {"identity": "tracklet:0", "player_id": "alice"})
        if entry == "engine":
            engine(
                [ENGINE, str(store.root), run_id],
            )
        else:
            call_command("republish_match_identity", run_id)
    assert published(store, run_id)["p1-raw-0"] == "alice"


@pytest.mark.parametrize("linked", ["unlinked", "late_substitute"])
def test_players_a_reviewer_adds_stay_on_the_match_roster(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    s3: MagicMock,
    linked: str,
) -> None:
    """Review round 3, finding 3: "Speler toevoegen" players are roster players.

    The recording is unlinked (an empty roster, which reviewers fill while
    naming) or its line-up misses a substitute. A reviewer adds the player in
    section 0 and confirms their view. The match pass read the roster from the
    replay's recipe only, so it counted the confirmation as outside the roster
    and published the view as unknown.
    """
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    owner, store, _ = imported
    run_id = replay_job(store, owner, [] if linked == "unlinked" else ROSTER[:3])
    with production(s3, {1: EVERYONE_BUT_DAVE}):
        run_section(store, run_id)
        run_section(store, run_id)
        AnalysisJob.objects.filter(pk=run_id).update(status="completed")
        state = answer(
            store,
            run_id,
            0,
            {
                "action": "add_player",
                "player": {"team": "team_b", "number": "5", "name": "Dave"},
            },
        )
        added = next(
            p["player_id"] for p in state["review"]["roster"] if p["number"] == "5"
        )
        answer(store, run_id, 0, {"identity": "tracklet:3", "player_id": added})
        tasks.republish_match_identity(run_id)
    names = published(store, run_id)
    assert names["p0-raw-3"] == added
    result = json.loads(
        (directory(store, run_id) / clip_match_wide.RESULT).read_text(encoding="utf-8")
    )
    assert (result["confirmations"], result["answers_outside_roster"]) == (1, 0)
    assert [p["player_id"] for p in result["roster_additions"]] == [added]


def test_a_stopped_recording_resumes_through_the_clip_api(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    s3: MagicMock,
) -> None:
    """Review round 3, finding 4, through the product path with object storage.

    Section 1's worker fails after publishing a partial chunk and its naming
    files. The job fails; the staff retry endpoint queues the same job, and the
    task resumes at section 1: the failed attempt's files are purged from
    object storage too (a stale review cache must not come back), section 0
    stays committed, and the match pass names both sections.
    """
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True
    owner, store, _ = imported
    run_id = replay_job(store, owner)
    client = verified(owner)
    csrf = client.get("/video-analysis/clips").json()["csrf"]

    def retry() -> int:
        return client.post(
            "/video-analysis/clips/retry",
            {"run_id": run_id},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code

    with (
        production(s3, {1: EVERYONE_BUT_DAVE}, fail={1}),
        patch("apps.video_analysis.services.jobs.enqueue") as enqueue,
    ):
        tasks.execute(run_id)
        assert AnalysisJob.objects.get(pk=run_id).status == "queued"
        assert retry() == HTTPStatus.CONFLICT
        tasks.execute(run_id)
        assert AnalysisJob.objects.get(pk=run_id).status == "failed"
        stale = StoredFile.objects.get(
            relative_path__endswith=f"{run_id}-part-0001/{clip_match_wide.REVIEW}"
        )
        partial = StoredFile.objects.get(
            relative_path__endswith=f"{run_id}/part-0001-chunk-00000.json"
        )
        assert retry() == HTTPStatus.ACCEPTED
        assert retry() == HTTPStatus.CONFLICT
        assert enqueue.call_args.kwargs["kwargs"] == {"retry": True}
        tasks.execute(run_id, retry=True)
    job = AnalysisJob.objects.get(pk=run_id)
    assert job.status == "completed"
    record = receipt(store, run_id)
    assert record["section_attempts"] == {"1": 1}
    assert [c["name"] for c in record["chunks"]] == [
        "part-0000-chunk-00000.json",
        "part-0001-r1-chunk-00000.json",
    ]
    again = StoredFile.objects.get(pk=partial.pk)
    assert (again.object_key, again.sha256) == (partial.object_key, partial.sha256)
    # The failed attempt's cache was deleted, not left for a later restore.
    s3.delete_object.assert_any_call(Bucket=stale.bucket, Key=stale.object_key)
    fresh = StoredFile.objects.get(
        relative_path__endswith=f"{run_id}-part-0001/{clip_match_wide.REVIEW}"
    )
    assert fresh.pk != stale.pk
    wide = record["match_identity_wide"]
    assert (wide["status"], wide["sections"]) == ("completed", SECTIONS)
    assert published(store, run_id)["p1-raw-0"] == "alice"
