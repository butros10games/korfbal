"""Roster naming for clip runs: auth, idempotent answers, revisions and crops."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from datetime import date
from http import HTTPStatus
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import MagicMock, patch
import uuid

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import Client
from django.utils import timezone
import pytest
from pytest_django.fixtures import Settings

from apps.competition.models import (
    Match as CompetitionMatch,
    MatchMembership,
    RosterMembership,
)
from apps.competition.tests.test_match_info import published_match
from apps.player.models import Player
from apps.schedule.models import Season
from apps.video_analysis import tasks
from apps.video_analysis.adapters import identity_review as adapter
from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.application.ports import IdentityReviewRuntime
from apps.video_analysis.composition import processing_store, worker_store
from apps.video_analysis.engine import clip_identity_review
from apps.video_analysis.engine.clip_closed_set import (
    Evidence,
    Player as RosterPlayer,
    ReviewSession,
    Scope,
    Settings as Recipe,
)
from apps.video_analysis.engine.clip_identity_crops import crop_name
from apps.video_analysis.engine.clip_identity_review import ReviewCache
from apps.video_analysis.engine.clip_match_evidence import fit_fragments
from apps.video_analysis.engine.clip_match_identity import Fragment
from apps.video_analysis.engine.clip_number_anchors import KitRead
from apps.video_analysis.engine.clip_section_identity import (
    GALLERY_FILE,
    GalleryLink,
    carried,
    open_gallery,
    remember,
)
from apps.video_analysis.engine.clips import directory
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.models import (
    AnalysisJob,
    ClipIdentityReview,
    IdentityReviewAnswer,
    Recording,
    Workspace,
)
from apps.video_analysis.services import identity_review
from apps.video_analysis.services.clips import start
from apps.video_analysis.tests.test_clips import ready
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db
np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
TWO_ANSWERS = 2
ROSTER = [RosterPlayer("alice", "team_a", "7"), RosterPlayer("bob", "team_a", "9")]


def piece(identity: str, time: float, vector: list[float]) -> Fragment:
    """One pure view in an already fitted shared match space."""
    return Fragment(
        identity, str(time), "team_a", {time}, samples=[np.array(vector)] * 6, weight=20
    )


def space(store: DatabaseStore) -> Workspace:
    """Load the store's workspace row."""
    return Workspace.objects.get(pk=store.workspace_id)


def clip_run(store: DatabaseStore, owner: User) -> AnalysisJob:
    """Create a finished clip run whose worker left a private naming cache."""
    ready(store)
    job = AnalysisJob.objects.create(
        workspace=space(store),
        requested_by=owner,
        kind="clip",
        status="completed",
        payload={"match_id": "demo", "options": {"start": 0, "duration": 40}},
    )
    root = directory(store, str(job.pk))
    root.mkdir(parents=True)
    atomic_json(root / "run.json", {"status": "completed", "chunks": []})
    scope = Scope("demo", str(job.pk))
    ReviewCache(root / "identity-review.sqlite", scope).create(
        ReviewSession(
            scope,
            [
                piece("a", 0, [1.0, 0.0]),
                piece("b", 0, [0.0, 1.0]),
                piece("c", 30, [1.0, 0.05]),
                piece("d", 30, [0.05, 1.0]),
            ],
            ROSTER,
            Recipe("synthetic-v1"),
        ),
        refinement={"status": "completed", "links": [], "frame_links": []},
    )
    return job


def in_process(store: DatabaseStore) -> IdentityReviewRuntime:
    """Run the real engine command in-process; crops are a fixed fake."""

    @contextmanager
    def processing(*_: object) -> AbstractContextManager[DatabaseStore]:
        yield store

    def solve(_: object, run_id: str, request: dict) -> dict:
        output = store.root / "identity-output.json"
        command = store.root / "identity-request.json"
        root = directory(store, run_id)
        atomic_json(
            command,
            {
                **request,
                "crops": None,
                "run_directory": str(root),
                "output": str(output),
            },
        )
        with patch.object(sys, "argv", ["review", str(command)]):
            clip_identity_review.main()
        result = json.loads(output.read_text(encoding="utf-8"))
        name = crop_name(30.0, "raw-c")
        (root / "identity-crops").mkdir(exist_ok=True)
        (root / "identity-crops" / name).write_bytes(b"\xff\xd8synthetic-jpeg")
        result["review"]["crops"] = {
            "c": [{"time_seconds": 30.0, "track_id": "raw-c", "crop": name}]
        }
        return result

    return IdentityReviewRuntime(processing_store=processing, solve=solve)


def drain(store: DatabaseStore, job: AnalysisJob) -> None:
    """Execute the queued vision-worker task with the in-process runtime."""
    with patch(
        "apps.video_analysis.composition.identity_review_runtime",
        return_value=in_process(store),
    ):
        tasks.review_identities(str(job.pk))


def post(client: Client, path: str, payload: dict, csrf: str) -> HttpResponse:
    """Post JSON with the session's CSRF token."""
    return client.post(
        f"/video-analysis/{path}",
        payload,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
    )


def test_naming_requires_staff_mfa_and_a_clip_of_this_workspace(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """JSON 401/403 instead of login redirects; other runs are not found."""
    owner, store, _ = imported
    job = clip_run(store, owner)
    path = f"/video-analysis/clips/identity?run={job.pk}"
    assert Client().get(path).status_code == HTTPStatus.UNAUTHORIZED
    unverified = Client()
    unverified.force_login(owner)
    response = unverified.get(path)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response["Content-Type"] == "application/json"
    client = verified(owner)
    assert client.get(path).json()["status"] == "unprepared"
    missing = client.get(f"/video-analysis/clips/identity?run={uuid.uuid4()}")
    assert missing.status_code == HTTPStatus.NOT_FOUND
    other = AnalysisJob.objects.create(
        workspace=space(store), requested_by=owner, kind="train", payload={}
    )
    assert (
        client.get(f"/video-analysis/clips/identity?run={other.pk}").status_code
        == HTTPStatus.NOT_FOUND
    )


def test_answers_queue_with_idempotency_and_structured_revision_conflicts(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A retry is harmless; a stale revision is a 409 the client can refresh to."""
    owner, store, _ = imported
    job = clip_run(store, owner)
    client = verified(owner)
    csrf = client.get(f"/video-analysis/clips/identity?run={job.pk}").json()["csrf"]
    with patch("apps.video_analysis.services.identity_review.enqueue") as publish:
        assert (
            post(client, "clips/identity/prepare", {"run_id": str(job.pk)}, csrf)
        ).status_code == HTTPStatus.ACCEPTED
        publish.assert_called_once()
    drain(store, job)
    state = client.get(f"/video-analysis/clips/identity?run={job.pk}").json()
    assert state["status"] == "ready"
    assert state["expected_revision"] == 0
    assert {q["identity"] for q in state["review"]["questions"]} == {
        "a",
        "b",
        "c",
        "d",
    }
    answer = {
        "run_id": str(job.pk),
        "request_id": str(uuid.uuid4()),
        "expected_revision": 0,
        "identity": "a",
        "player_id": "alice",
    }
    with patch("apps.video_analysis.services.identity_review.enqueue") as publish:
        first = post(client, "clips/identity/answer", answer, csrf)
        retry = post(client, "clips/identity/answer", answer, csrf)
        assert first.status_code == HTTPStatus.ACCEPTED
        assert retry.status_code == HTTPStatus.OK
        assert retry.json()["request_id"] == answer["request_id"]
        assert publish.call_count == 1
        # A second answer pipelines on the first one's expected revision.
        second = dict(
            answer, request_id=str(uuid.uuid4()), expected_revision=1, identity="b"
        )
        second["player_id"] = "bob"
        assert (
            post(client, "clips/identity/answer", second, csrf).status_code
            == HTTPStatus.ACCEPTED
        )
        stale = dict(answer, request_id=str(uuid.uuid4()), identity="c")
        conflict = post(client, "clips/identity/answer", stale, csrf)
        assert conflict.status_code == HTTPStatus.CONFLICT
        body = conflict.json()
        assert {k: body[k] for k in ("code", "revision", "pending")} == {
            "code": "revision_conflict",
            "revision": 0,
            "pending": TWO_ANSWERS,
        }
        assert body["expected_revision"] == TWO_ANSWERS
        reused = dict(answer, identity="d")
        assert (
            post(client, "clips/identity/answer", reused, csrf).status_code
            == HTTPStatus.CONFLICT
        )
    drain(store, job)
    state = client.get(f"/video-analysis/clips/identity?run={job.pk}").json()
    assert state["revision"] == state["expected_revision"] == TWO_ANSWERS
    fragments = state["review"]["fragments"]
    assert fragments["a"]["naming"] == "confirmed"
    assert fragments["c"] == {
        "naming": "automatic",
        "player_id": "alice",
        "display_id": "#7",
        "margin": fragments["c"]["margin"],
        "unclear": False,
    }
    assert state["review"]["summary"]["players_confirmed"] == TWO_ANSWERS
    assert [a["status"] for a in state["answers"]] == ["applied", "applied"]


def test_worker_rejects_stale_answers_alone_and_keeps_free_form_names(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Free-form players get names; an answer the engine refuses is reported."""
    owner, store, _ = imported
    job = clip_run(store, owner)
    with patch("apps.video_analysis.services.identity_review.enqueue"):
        identity_review.prepare(store, space(store), str(job.pk))
        drain(store, job)
        identity_review.submit(
            space(store),
            owner,
            {
                "run_id": str(job.pk),
                "request_id": str(uuid.uuid4()),
                "expected_revision": 0,
                "action": "add_player",
                "player": {"team": "team_a", "number": "12", "name": "Sanne"},
            },
        )
        identity_review.submit(
            space(store),
            owner,
            {
                "run_id": str(job.pk),
                "request_id": str(uuid.uuid4()),
                "expected_revision": 1,
                "action": "dismiss",
                "identity": "nope",
                "reason": "referee",
            },
        )
    drain(store, job)
    state = identity_review.read(store, space(store), str(job.pk))
    assert state["revision"] == 1
    added = next(r for r in state["review"]["roster"] if r["number"] == "12")
    assert state["review"]["names"][added["player_id"]] == "Sanne"
    rejected = state["answers"][0]
    assert (rejected["status"], rejected["code"]) == ("rejected", "invalid")


def test_crops_are_served_only_when_listed_by_the_run(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Crops reuse the private media path and never accept arbitrary names."""
    owner, store, _ = imported
    job = clip_run(store, owner)
    with patch("apps.video_analysis.services.identity_review.enqueue"):
        identity_review.prepare(store, space(store), str(job.pk))
    drain(store, job)
    client = verified(owner)
    name = crop_name(30.0, "raw-c")
    response = client.get(
        f"/video-analysis/clips/identity/crop?run={job.pk}&name={name}"
    )
    assert response.status_code == HTTPStatus.OK
    assert response["Content-Type"] == "image/jpeg"
    assert response["Cache-Control"] == "private, no-store"
    assert b"".join(response.streaming_content) == b"\xff\xd8synthetic-jpeg"
    for bad in ("../run.json", crop_name(0.0, "raw-a"), "identity-review.sqlite"):
        assert (
            client.get(
                f"/video-analysis/clips/identity/crop?run={job.pk}&name={bad}"
            ).status_code
            == HTTPStatus.NOT_FOUND
        )


def test_prepare_requires_a_naming_cache(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Runs analysed without a roster report naming as unavailable."""
    owner, store, _ = imported
    ready(store)
    job = AnalysisJob.objects.create(
        workspace=space(store),
        requested_by=owner,
        kind="clip",
        status="completed",
        payload={"match_id": "demo"},
    )
    assert identity_review.read(store, space(store), str(job.pk))["status"] == (
        "unavailable"
    )
    with pytest.raises(FileNotFoundError):
        identity_review.prepare(store, space(store), str(job.pk))


def test_worker_failure_keeps_the_previous_snapshot(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A crashed re-solve rejects its batch; reviewers can still read and retry."""
    owner, store, _ = imported
    job = clip_run(store, owner)
    with patch("apps.video_analysis.services.identity_review.enqueue"):
        identity_review.prepare(store, space(store), str(job.pk))
        drain(store, job)
        identity_review.submit(
            space(store),
            owner,
            {
                "run_id": str(job.pk),
                "request_id": str(uuid.uuid4()),
                "expected_revision": 0,
                "identity": "a",
                "player_id": "alice",
            },
        )
    broken = IdentityReviewRuntime(
        processing_store=in_process(store).processing_store,
        solve=lambda *_: (_ for _ in ()).throw(OSError("worker died")),
    )
    identity_review.process(str(job.pk), broken)
    state = identity_review.read(store, space(store), str(job.pk))
    assert (state["status"], state["revision"], state["pending"]) == ("ready", 0, 0)
    assert state["answers"][0]["code"] == "failed"


def test_subprocess_adapter_runs_the_engine_command_in_the_vision_runtime(
    imported: tuple[User, DatabaseStore, Store], settings: Settings
) -> None:
    """Web code never imports the solver; the worker spawns the pinned runtime."""
    _, store, _ = imported
    settings.VIDEO_ANALYSIS_PYTHON = "/opt/vision/bin/python"

    def fake(command: list[str], **options: object) -> None:
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        assert command[:3] == [
            "/opt/vision/bin/python",
            "-m",
            "apps.video_analysis.engine.clip_identity_review",
        ]
        assert options["timeout"] == adapter.TIMEOUT_SECONDS
        assert request["run_directory"].endswith("vision/clips/run-1")
        Path(request["output"]).write_text(
            '{"review": {}, "answers": []}', encoding="utf-8"
        )

    with patch.object(adapter.subprocess, "run", side_effect=fake):
        assert adapter.solve(store, "run-1", {"answers": []}) == {
            "review": {},
            "answers": [],
        }


def test_clip_start_builds_the_linked_match_roster(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Selected players with team-unique canonical shirt numbers, home as team_a."""
    owner, store, _ = imported
    season = Season.objects.create(
        name="2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )
    native = published_match(season)
    competition = CompetitionMatch.objects.get(local_match=native)
    now = timezone.now()
    rows = [
        ("Anna", competition.home_team, "7", "starter"),
        ("Bram", competition.home_team, "07", "starter"),
        ("Coach", competition.home_team, "", "staff"),
        ("Dirk", competition.away_team, "7", "substitute"),
    ]
    for name, team, shirt, role in rows:
        player = Player.objects.create(name=name)
        MatchMembership.objects.create(
            match=competition, team=team, player=player, role=role, observed_at=now
        )
        RosterMembership.objects.create(
            player=player,
            team=team,
            first_seen_at=now,
            last_seen_at=now,
            shirt_number=shirt,
            roles=["PLAYER_DEFAULT"],
        )
    payload = ready(store)
    Recording.objects.filter(source_id="demo").update(match=native)
    with patch("apps.video_analysis.services.jobs.enqueue"):
        job = start(store, space(store), owner, {**payload, "name_players": True})
    roster = job.payload["options"]["match_identity"]["closed_set"]["roster"]
    names = {str(p.pk): p.name for p in Player.objects.all()}
    assert sorted((names[r["player_id"]], r["team"], r["number"]) for r in roster) == [
        ("Anna", "team_a", "7"),
        ("Bram", "team_a", None),
        ("Dirk", "team_b", "7"),
    ]
    unlinked = ready_unlinked(store, owner)
    assert unlinked.payload["options"]["match_identity"]["closed_set"]["roster"] == []


@pytest.mark.usefixtures("imported")
def test_a_shirt_number_two_teammates_share_names_neither() -> None:
    """Duplicate team numbers are dropped for both players, not kept for one.

    Review v1 finding 5: the alphabetically first player kept #7, so a correct
    #7 read on the other one became a false automatic anchor.
    """
    season = Season.objects.create(
        name="2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )
    native = published_match(season)
    competition = CompetitionMatch.objects.get(local_match=native)
    now = timezone.now()
    rows = [
        ("Alice", competition.home_team, "7"),
        ("Bob", competition.home_team, "7"),
        ("Eva", competition.home_team, "9"),
        ("Dirk", competition.away_team, "7"),
    ]
    for name, team, shirt in rows:
        player = Player.objects.create(name=name)
        MatchMembership.objects.create(
            match=competition, team=team, player=player, role="starter", observed_at=now
        )
        RosterMembership.objects.create(
            player=player,
            team=team,
            first_seen_at=now,
            last_seen_at=now,
            shirt_number=shirt,
            roles=["PLAYER_DEFAULT"],
        )
    Recording.objects.filter(source_id="demo").update(match=native)
    roster = identity_review.match_roster(Recording.objects.get(source_id="demo"))
    names = {str(p.pk): p.name for p in Player.objects.all()}
    assert sorted((names[r["player_id"]], r["team"], r["number"]) for r in roster) == [
        ("Alice", "team_a", None),
        ("Bob", "team_a", None),
        ("Dirk", "team_b", "7"),
        ("Eva", "team_a", "9"),
    ]


def ready_unlinked(store: DatabaseStore, owner: User) -> AnalysisJob:
    """Start a free-form naming run for a recording without a linked match."""
    Recording.objects.filter(source_id="demo").update(match=None)
    AnalysisJob.objects.filter(status="queued").update(status="completed")
    payload = {
        "request_id": str(uuid.uuid4()),
        "model": "model",
        "match_id": "demo",
        "options": {"start": 10, "duration": 20},
        "name_players": True,
    }
    with patch("apps.video_analysis.services.jobs.enqueue"):
        return start(store, space(store), owner, payload)


def test_finished_clip_with_a_roster_prepares_naming(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The worker queues the first snapshot once the clip receipt is durable."""
    owner, store, _ = imported
    job = clip_run(store, owner)
    job.payload["options"]["match_identity"] = {"closed_set": {"roster": []}}
    with patch("apps.video_analysis.services.identity_review.enqueue") as publish:
        identity_review.after_clip(job)
    publish.assert_called_once()
    assert IdentityReviewAnswer.objects.count() == 0
    assert ClipIdentityReview.objects.get(job=job, section="").status == "preparing"


def test_read_matches_the_shared_web_contract_fixture(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """The panel's TypeScript schema and this response share one fixture."""
    owner, store, _ = imported
    job = clip_run(store, owner)
    with patch("apps.video_analysis.services.identity_review.enqueue"):
        identity_review.prepare(store, space(store), str(job.pk))
    drain(store, job)
    fixture = json.loads(
        (
            Path(__file__).parents[6] / "fixtures/korfbal/vision-closed-set.json"
        ).read_text(encoding="utf-8")
    )["naming_state"]
    state = identity_review.read(store, space(store), str(job.pk))
    assert set(state) == set(fixture) - {"csrf"}
    assert set(state["review"]) == set(fixture["review"])
    assert set(state["review"]["summary"]) == set(fixture["review"]["summary"])
    fragment = next(iter(state["review"]["fragments"].values()))
    assert set(fragment) == set(next(iter(fixture["review"]["fragments"].values())))


def test_an_answer_whose_re_solve_runs_out_of_time_stays_unapplied(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Review v2 finding 12: the worker must not mark a stopped solve applied.

    The engine solver stopped at its deadline, kept the old revision and
    installed nothing, yet the stored answer said ``applied``, so the client
    dropped the reviewer's correction and a retry returned the stored receipt.
    """
    owner, store, _ = imported
    job = clip_run(store, owner)
    with patch("apps.video_analysis.services.identity_review.enqueue"):
        identity_review.prepare(store, space(store), str(job.pk))
        drain(store, job)
        identity_review.submit(
            space(store),
            owner,
            {
                "run_id": str(job.pk),
                "request_id": str(uuid.uuid4()),
                "expected_revision": 0,
                "identity": "a",
                "player_id": "alice",
            },
        )
    load = ReviewCache.load

    def out_of_time(self: ReviewCache, db: sqlite3.Connection) -> ReviewSession:
        # The review restores in time; the answer's re-solve then runs past the
        # review's own deadline (it travels with the review's settings).
        loaded = load(self, db)
        loaded.settings = replace(loaded.settings, deadline=-1)
        return loaded

    with (
        patch.object(ReviewCache, "load", out_of_time),
        patch("apps.video_analysis.services.identity_review.enqueue"),
    ):
        drain(store, job)
    state = identity_review.read(store, space(store), str(job.pk))
    assert (state["status"], state["revision"], state["pending"]) == ("ready", 0, 0)
    assert state["expected_revision"] == 0
    assert state["review"]["anchors"] == []
    answer = state["answers"][0]
    assert (answer["status"], answer["code"], answer["revision"]) == (
        "rejected",
        "timed_out",
        0,
    )
    assert "nothing changed" in answer["message"]


# Long recordings: a replay's sections are child runs reviewed under their run ID.
RECIPE = "synthetic-v1"
FOUR = [
    RosterPlayer("alice", "team_a", "7"),
    RosterPlayer("bob", "team_a", "9"),
    RosterPlayer("carol", "team_a", "4"),
    RosterPlayer("dave", "team_a", "5"),
]


def raw_view(identity: str, time: float, who: int) -> Fragment:
    """Build a pure view with raw descriptors around one person's prototype."""
    rng = np.random.default_rng(who)
    centre = np.eye(16)[who]
    raw = (centre + 0.05 * rng.standard_normal((12, 16))).astype(np.float16)
    return Fragment(identity, "0", "team_a", {time}, raw={identity: raw}, weight=20)


def replay_section(store: DatabaseStore, owner: User) -> tuple[AnalysisJob, str]:
    """Create a replay whose first section named Alice's view as Bob (a misread).

    Alice's tracklet pools as #9 (Bob's number) and Carol's as #4: two players
    orient the kits, and Alice's descriptors are committed to the replay's
    gallery under Bob.

    Returns:
        The replay job and its first section's run ID.

    """
    ready(store)
    job = AnalysisJob.objects.create(
        workspace=space(store),
        requested_by=owner,
        kind="clip",
        status="running",
        payload={
            "match_id": "demo",
            "recording_end": 250,
            "options": {"start": 0, "duration": 120},
        },
    )
    run_id = f"{job.pk}-part-0000"
    root = directory(store, run_id)
    root.mkdir(parents=True)
    atomic_json(root / "run.json", {"status": "completed", "chunks": []})
    gallery = open_gallery(directory(store, str(job.pk)) / GALLERY_FILE, "demo", RECIPE)
    assert gallery is not None
    pieces = [
        raw_view(n, i, i) for i, n in enumerate(("alice", "bob", "carol", "dave"))
    ]
    fit_fragments(pieces, dimensions=8)
    scope = Scope("demo", run_id)
    session = ReviewSession(
        scope,
        pieces,
        FOUR,
        Recipe(RECIPE),
        Evidence(
            numbers=(
                KitRead("alice", "team_a", "9", 0.9),
                KitRead("carol", "team_a", "4", 0.9),
            ),
            section="part-0000",
        ),
    )
    span = {"id": "part-0000", "start": 0.0, "end": 120.0, "fingerprint": "f0"}
    remember(gallery, span, pieces, session.result, FOUR, session.sightings())
    ReviewCache(root / "identity-review.sqlite", scope).create(
        session,
        refinement={"status": "completed", "links": [], "frame_links": []},
        gallery=GalleryLink(str(job.pk), "part-0000", RECIPE),
    )
    return job, run_id


def gallery_name(store: DatabaseStore, job: AnalysisJob, view: str) -> tuple:
    """Return who the replay gallery names a committed view as, and how."""
    gallery = open_gallery(directory(store, str(job.pk)) / GALLERY_FILE, "demo", RECIPE)
    assert gallery is not None
    _, anchors = carried(gallery, FOUR)
    anchor = next(a for a in anchors if a.identity.endswith(f":{view}"))
    return anchor.player_id, anchor.source


def without_crops(store: Store, run_id: str, request: dict) -> dict:
    """Run the real engine command in-process, as the vision worker would."""
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


def test_replay_sections_are_named_through_the_clip_endpoints(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Review v2 finding 4: a section's corrections must be reachable.

    Sections are child runs without a job row; the endpoints accepted only
    UUID job IDs, so nothing could correct the gallery views a section
    committed. They now take ``<replay>-part-NNNN`` under the same checks.
    """
    owner, store, _ = imported
    job, run_id = replay_section(store, owner)
    client = verified(owner)
    replay = client.get(f"/video-analysis/clips/identity?run={job.pk}").json()
    assert (replay["status"], replay["sections"]) == ("unavailable", [run_id])
    state = client.get(f"/video-analysis/clips/identity?run={run_id}").json()
    assert state["status"] == "unprepared"
    plain = AnalysisJob.objects.create(
        workspace=space(store),
        requested_by=owner,
        kind="clip",
        status="completed",
        payload={"match_id": "demo"},
    )
    for bad in (f"{job.pk}-part-12", f"{plain.pk}-part-0000", f"{uuid.uuid4()}"):
        response = client.get(f"/video-analysis/clips/identity?run={bad}")
        assert response.status_code == HTTPStatus.NOT_FOUND, bad
    with patch("apps.video_analysis.services.identity_review.enqueue") as publish:
        response = post(
            client, "clips/identity/prepare", {"run_id": run_id}, state["csrf"]
        )
        assert response.status_code == HTTPStatus.ACCEPTED
        assert publish.call_args.kwargs["args"] == [run_id]
    runtime = IdentityReviewRuntime(
        processing_store=in_process(store).processing_store, solve=without_crops
    )
    identity_review.process(run_id, runtime)
    state = client.get(f"/video-analysis/clips/identity?run={run_id}").json()
    assert state["status"] == "ready"
    assert state["review"]["fragments"]["alice"]["player_id"] == "bob"
    answer = {
        "run_id": run_id,
        "request_id": str(uuid.uuid4()),
        "expected_revision": 0,
        "identity": "alice",
        "player_id": "alice",
    }
    with patch("apps.video_analysis.services.identity_review.enqueue"):
        queued = post(client, "clips/identity/answer", answer, state["csrf"])
    assert queued.status_code == HTTPStatus.ACCEPTED
    assert IdentityReviewAnswer.objects.get().section == "part-0000"
    identity_review.process(run_id, runtime)
    state = client.get(f"/video-analysis/clips/identity?run={run_id}").json()
    assert (state["revision"], state["answers"][0]["status"]) == (1, "applied")
    assert gallery_name(store, job, "alice") == ("alice", "recording_gallery")
    # The replay's own (section-less) review is untouched.
    assert not ClipIdentityReview.objects.filter(job=job, section="").exists()


def test_the_review_worker_restores_and_publishes_the_replay_gallery(
    imported: tuple[User, DatabaseStore, Store],
    settings: Settings,
    s3: MagicMock,
) -> None:
    """With object storage the gallery is evicted between jobs.

    The worker must restore it before the section's solve (else corrections
    never reach it) and publish the corrected bytes before storing the answer
    as applied (else the next section restores the uncorrected gallery).
    """
    owner, store, _ = imported
    job, run_id = replay_section(store, owner)
    settings.VIDEO_ANALYSIS_OBJECT_STORAGE = True

    @contextmanager
    def no_video(*_: object) -> Iterator[str]:
        yield "unused-by-a-review-without-crops"

    with (
        patch("apps.video_analysis.adapters.objects.object_client", return_value=s3),
        patch(
            "apps.video_analysis.adapters.objects.WorkspaceObjects.video_source",
            no_video,
        ),
    ):
        with processing_store(space(store), None):
            pass
        gallery = directory(store, str(job.pk)) / GALLERY_FILE
        assert not gallery.exists()
        ClipIdentityReview.objects.create(
            job=job, section="part-0000", status="ready", snapshot={"revision": 0}
        )
        with patch("apps.video_analysis.services.identity_review.enqueue"):
            identity_review.submit(
                space(store),
                owner,
                {
                    "run_id": run_id,
                    "request_id": str(uuid.uuid4()),
                    "expected_revision": 0,
                    "identity": "alice",
                    "player_id": "alice",
                },
            )
        runtime = IdentityReviewRuntime(
            processing_store=processing_store, solve=without_crops
        )
        identity_review.process(run_id, runtime)
        state = identity_review.read(store, space(store), run_id)
        assert (state["revision"], state["answers"][0]["status"]) == (1, "applied")
        assert not gallery.exists()
        worker = worker_store(space(store), None, hydrate=False)
        worker.media(gallery.relative_to(store.root).as_posix())
        assert gallery_name(store, job, "alice") == ("alice", "recording_gallery")
