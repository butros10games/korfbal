"""Name players in a clip run: answers are stored here, solved by the vision worker.

A clip run started with roster naming leaves a private descriptor cache in its
run directory. Web requests never open it: they validate and queue answers with
an idempotency key and an expected revision, and read the compact snapshot the
worker stores after each re-solve. The worker applies queued answers in order,
crops the next questions from the immutable chunks and publishes both.

A long recording is one replay job whose sections are child runs
(``<replay>-part-NNNN``), each with its own cache; they are reviewed through the
same endpoints under that run ID. A section's answers also correct the replay's
shared identity gallery, so the worker restores the gallery before solving and
publishes it before the answers are stored as applied.
"""

from __future__ import annotations

from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
import json
import logging
from pathlib import Path
import re
import subprocess
from typing import TYPE_CHECKING, Any
import uuid

from django.db import transaction
from django.utils import timezone

from apps.competition.models import (
    Match as CompetitionMatch,
    MatchMembership,
    RosterMembership,
)
from apps.game_tracker.models import MatchPlayer
from apps.kwt_common.services.jobs import enqueue
from apps.player.models import Player
from apps.video_analysis.engine.clip_section_identity import GALLERY_FILE
from apps.video_analysis.engine.clips import directory
from apps.video_analysis.engine.store import ConflictError, Store
from apps.video_analysis.models import (
    AnalysisJob,
    ClipIdentityReview,
    IdentityReviewAnswer,
    Recording,
    StoredFile,
    Workspace,
)


if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from apps.schedule.models import Match
    from apps.video_analysis.application.ports import IdentityReviewRuntime


logger = logging.getLogger(__name__)

CACHE = "identity-review.sqlite"
CROPS = "identity-crops"
ACTIONS = {"confirm", "dismiss", "remove", "add_player"}
REASONS = {"not_player", "referee", "unclear"}
MAX_QUEUED = 32
MAX_ROSTER = 64
MAX_IDENTITY = 200
MAX_NAME = 60
RECENT = 20
CROP_NAME = re.compile(r"^[0-9a-f]{24}\.jpg$")
SECTION = re.compile(r"^part-[0-9]{4}$")
SECTION_MARK = "-part-"
SHIRT = re.compile(r"^(0|[1-9][0-9]?)$")
PLAYER_ROLE = "PLAYER_DEFAULT"
TASK = "apps.video_analysis.tasks.review_identities"
MATCH_TASK = "apps.video_analysis.tasks.republish_match_identity"
# Reviewers answer in bursts; one match pass a little later covers all of them.
MATCH_DEBOUNCE = timedelta(seconds=30)


class RevisionConflictError(ConflictError):
    """The answer expected an older review; carries the current revision."""

    def __init__(self, revision: int, pending: int) -> None:
        """Describe the review state the client must refresh to."""
        super().__init__("Review changed; refresh before answering")
        self.revision, self.pending = revision, pending


@dataclass(frozen=True)
class Target:
    """What is under review: a clip run, or one section of a replay job."""

    job: AnalysisJob
    section: str = ""

    @property
    def run_id(self) -> str:
        """The run directory: the clip run's, or the section's child run."""
        return f"{self.job.pk}-{self.section}" if self.section else str(self.job.pk)


def parse_run(run_id: str) -> tuple[uuid.UUID, str]:
    """Split ``<job>`` or ``<job>-part-NNNN`` into the job and the section.

    Returns:
        The job's primary key and the section (empty for a clip run).

    Raises:
        FileNotFoundError: The suffix is not a section.

    """
    job_id, mark, part = run_id.partition(SECTION_MARK)
    section = f"part-{part}" if mark else ""
    if mark and not SECTION.match(section):
        raise FileNotFoundError("Clip not found")
    return uuid.UUID(job_id), section


def clip_target(workspace: Workspace, run_id: str) -> Target:
    """Authorize a clip run, or a replay section, of this workspace first.

    Returns:
        The run's durable job and section.

    Raises:
        FileNotFoundError: The run is not a clip run (or a section of a replay
            clip run) of this workspace.

    """
    job_id, section = parse_run(run_id)
    job = AnalysisJob.objects.filter(
        workspace=workspace, pk=job_id, kind="clip"
    ).first()
    if job is None or (section and not job.payload.get("recording_end")):
        raise FileNotFoundError("Clip not found")
    return Target(job, section)


def relative(store: Store, target: Target, name: str) -> str:
    """Return a run-scoped workspace path; ``name`` is never client-chosen.

    Returns:
        The artifact's workspace-relative path.

    """
    return (directory(store, target.run_id) / name).relative_to(store.root).as_posix()


def has_cache(store: Store, workspace: Workspace, target: Target) -> bool:
    """Report whether the run was analysed with roster naming enabled.

    Returns:
        Whether the private descriptor cache was published or is local.

    """
    path = relative(store, target, CACHE)
    return (store.root / path).is_file() or StoredFile.objects.filter(
        workspace=workspace, relative_path=path
    ).exists()


def sections(store: Store, workspace: Workspace, job: AnalysisJob) -> list[str]:
    """List a replay's sections that can be named, as reviewable run IDs.

    Returns:
        Sorted section run IDs that have a naming cache.

    """
    if not job.payload.get("recording_end"):
        return []
    prefix = f"{directory(store, str(job.pk)).relative_to(store.root).as_posix()}-part-"
    names = {
        path.split("/")[-2]
        for path in StoredFile.objects.filter(
            workspace=workspace,
            relative_path__startswith=prefix,
            relative_path__endswith=f"/{CACHE}",
        ).values_list("relative_path", flat=True)
    }
    root = directory(store, str(job.pk))
    names.update(
        path.parent.name for path in root.parent.glob(f"{root.name}-part-*/{CACHE}")
    )
    return sorted(
        name for name in names if SECTION.match(name.removeprefix(f"{job.pk}-"))
    )


def match_roster(recording: Recording) -> list[dict[str, Any]]:
    """Build the closed-set roster of the linked match, home as team_a.

    Prefers the provider match selection, then the native lineup, then the
    teams' active provider rosters. Shirt numbers come from roster memberships;
    invalid numbers, and numbers two teammates share, are dropped for every
    player concerned, never invented or given to one of them. Privacy-hidden
    players are omitted (``Player.objects``); reviewers can add them by hand.

    Returns:
        Up to 64 roster rows; empty when the recording has no linked match.

    """
    match = recording.match
    if match is None:
        return []
    competition = CompetitionMatch.objects.filter(local_match=match).first()
    teams = (
        {competition.home_team_id: "team_a", competition.away_team_id: "team_b"}
        if competition
        else {}
    )
    sides: dict[uuid.UUID, str] = {}
    if competition:
        sides = {
            player_id: teams[team_id]
            for player_id, team_id in MatchMembership.objects
            .filter(match=competition)
            .exclude(role="staff")
            .values_list("player_id", "team_id")
        }
    if not sides:
        native = {match.home_team_id: "team_a", match.away_team_id: "team_b"}
        sides = {
            uuid.UUID(str(player_id)): native[team_id]
            for player_id, team_id in MatchPlayer.objects.filter(
                match_data__match_link=match
            ).values_list("player_id", "team_id")
            if team_id in native
        }
    memberships = list(
        RosterMembership.objects.filter(
            team_id__in=list(teams), ended_at__isnull=True
        ).values_list("player_id", "team_id", "shirt_number", "roles")
    )
    if not sides:
        # Without a match selection, use the teams' active players.
        sides = {
            player_id: teams[team_id]
            for player_id, team_id, _, roles in memberships
            if isinstance(roles, list) and PLAYER_ROLE in roles
        }
    numbers = {
        player_id: shirt.strip()
        for player_id, team_id, shirt, _ in memberships
        if sides.get(player_id) == teams[team_id] and shirt
    }
    visible = {
        p.pk: p.display_name
        for p in Player.objects.filter(pk__in=list(sides)).select_related("user")
    }
    chosen = sorted(
        ((pid, side) for pid, side in sides.items() if pid in visible),
        key=lambda row: (row[1], visible[row[0]]),
    )[:MAX_ROSTER]
    # A number two teammates share is ambiguous: a read of it must name neither,
    # so neither keeps it (the other team may wear the same number).
    worn = Counter(
        (side, numbers.get(player_id, ""))
        for player_id, side in chosen
        if SHIRT.match(numbers.get(player_id, ""))
    )
    roster: list[dict[str, Any]] = []
    for player_id, side in chosen:
        shirt = numbers.get(player_id, "")
        number = shirt if SHIRT.match(shirt) and worn[side, shirt] == 1 else None
        roster.append({"player_id": str(player_id), "team": side, "number": number})
    return roster


def team_labels(recording: Recording | None) -> dict[str, str]:
    """Label roster teams by the linked match, else neutrally.

    Returns:
        Display names for team_a and team_b.

    """
    match: Match | None = recording.match if recording else None
    if match is None:
        return {"team_a": "Team A", "team_b": "Team B"}
    return {"team_a": str(match.home_team), "team_b": str(match.away_team)}


def player_names(job: AnalysisJob, roster: list[dict]) -> dict[str, str]:
    """Resolve visible native names and reviewer-entered free-form names.

    Returns:
        Display names keyed by roster player ID.

    """
    names: dict[str, str] = {}
    for answer in (
        IdentityReviewAnswer.objects
        .filter(job=job)
        .filter(status="applied")
        .order_by("sequence")
    ):
        player = answer.payload.get("player") or {}
        if answer.payload.get("action") == "add_player" and answer.payload.get("name"):
            names[player.get("player_id", "")] = answer.payload["name"]
    ids = []
    for row in roster:
        with suppress(ValueError):
            ids.append(uuid.UUID(row["player_id"]))
    for player in Player.objects.filter(pk__in=ids).select_related("user"):
        names[str(player.pk)] = player.display_name
    return names


def answer_row(answer: IdentityReviewAnswer) -> dict:
    """Expose a receipt without other reviewers' identities.

    Returns:
        The answer's action, target and worker receipt.

    """
    return {
        "request_id": str(answer.pk),
        "sequence": answer.sequence,
        "action": answer.payload.get("action", "confirm"),
        "identity": answer.payload.get("identity"),
        "player_id": answer.payload.get("player_id")
        or (answer.payload.get("player") or {}).get("player_id"),
        "reason": answer.payload.get("reason"),
        "status": answer.status,
        "code": answer.code,
        "message": answer.message,
        "revision": answer.revision,
    }


def read(store: Store, workspace: Workspace, run_id: str) -> dict:
    """Read the latest snapshot, queued answers and names for one run.

    Returns:
        Review state for the naming panel; never descriptors or frame aliases.
        A replay lists its reviewable sections.

    """
    target = clip_target(workspace, run_id)
    job, section = target.job, target.section
    review = ClipIdentityReview.objects.filter(job=job, section=section).first()
    recording = (
        Recording.objects
        .select_related("match__home_team", "match__away_team")
        .filter(workspace=workspace, source_id=job.payload.get("match_id"))
        .first()
    )
    answers = IdentityReviewAnswer.objects.filter(job=job, section=section)
    pending = answers.filter(status="queued").count()
    snapshot = dict(review.snapshot) if review else {}
    if snapshot:
        snapshot["names"] = player_names(job, snapshot.get("roster", []))
    status = (
        review.status
        if review
        else "unprepared"
        if has_cache(store, workspace, target)
        else "unavailable"
    )
    revision = review.revision if review else 0
    return {
        "status": status,
        "message": review.message if review else "",
        "revision": revision,
        "pending": pending,
        "expected_revision": revision + pending,
        "teams": team_labels(recording),
        "linked_match": bool(recording and recording.match_id),
        "review": snapshot or None,
        "answers": [answer_row(a) for a in answers.order_by("-created_at")[:RECENT]],
        "sections": [] if section else sections(store, workspace, job),
    }


def crop_path(store: Store, workspace: Workspace, run_id: str, name: str) -> str:
    """Authorize a crop named by this run's current snapshot.

    Returns:
        The crop's workspace-relative path.

    Raises:
        FileNotFoundError: The crop is not part of this run's review.

    """
    target = clip_target(workspace, run_id)
    review = ClipIdentityReview.objects.filter(
        job=target.job, section=target.section
    ).first()
    listed = {
        view.get("crop")
        for views in ((review.snapshot if review else {}).get("crops") or {}).values()
        for view in views
    }
    if not CROP_NAME.match(name) or name not in listed:
        raise FileNotFoundError("Crop not found")
    return relative(store, target, f"{CROPS}/{name}")


def new_player_fields(payload: dict, request_id: uuid.UUID) -> dict:
    """Normalize a free-form roster addition; the ID derives from the request.

    Returns:
        The stored add-player answer, including its display name.

    Raises:
        ValueError: The player is malformed.

    """
    player = payload.get("player")
    if not isinstance(player, dict) or player.get("team") not in {"team_a", "team_b"}:
        raise ValueError("Choose team A or B for the new player")
    number = player.get("number") or None
    name = str(player.get("name") or "").strip()[:MAX_NAME]
    if number is not None and (not isinstance(number, str) or not SHIRT.match(number)):
        raise ValueError("A shirt number has one or two digits")
    if number is None and not name:
        raise ValueError("Enter a shirt number or a name")
    return {
        "action": "add_player",
        "player": {
            "player_id": f"free-{request_id.hex[:12]}",
            "team": player["team"],
            "number": number,
        },
        "name": name,
    }


def answer_fields(payload: dict, request_id: uuid.UUID) -> dict:
    """Normalize one answer to the engine's action fields (plus a display name).

    Returns:
        The stored answer payload.

    Raises:
        ValueError: The answer is malformed.

    """
    action = payload.get("action", "confirm")
    if action not in ACTIONS:
        raise ValueError("Choose confirm, dismiss, remove or add a player")
    if action == "add_player":
        return new_player_fields(payload, request_id)
    identity = payload.get("identity")
    if not isinstance(identity, str) or not 1 <= len(identity) <= MAX_IDENTITY:
        raise ValueError("Choose a tracked view")
    fields: dict[str, Any] = {"action": action, "identity": identity}
    if action == "confirm":
        player_id = payload.get("player_id")
        if not isinstance(player_id, str) or not 1 <= len(player_id) <= MAX_ROSTER:
            raise ValueError("Choose a player")
        fields["player_id"] = player_id
    elif action == "dismiss":
        if payload.get("reason") not in REASONS:
            raise ValueError("Choose not a player, referee or cannot tell")
        fields["reason"] = payload["reason"]
    return fields


@transaction.atomic
def submit(workspace: Workspace, actor: User, payload: dict) -> tuple[int, dict]:
    """Queue one idempotent answer at the revision the reviewer saw.

    Returns:
        HTTP status and receipt: 202 when queued, 200 for a known retry.

    Raises:
        ConflictError: The request ID was used for another answer.
        RevisionConflictError: The reviewer's revision is stale.
        ValueError: The answer or the review is not usable.

    """
    target = clip_target(workspace, str(payload.get("run_id", "")))
    job, section = target.job, target.section
    review = (
        ClipIdentityReview.objects
        .select_for_update()
        .filter(job=job, section=section)
        .first()
    )
    if review is None or review.status == "preparing" or not review.snapshot:
        raise ValueError("Player naming is still being prepared")
    request_id = uuid.UUID(str(payload.get("request_id", "")))
    fields = answer_fields(payload, request_id)
    expected = payload.get("expected_revision")
    existing = IdentityReviewAnswer.objects.filter(pk=request_id).first()
    if existing:
        if (
            existing.job_id != job.pk
            or existing.section != section
            or existing.requested_by_id != actor.pk
            or existing.payload != fields
            or existing.sequence != expected
        ):
            raise ConflictError("Request ID already used")
        return 200, {"queued": existing.status == "queued", **answer_row(existing)}
    pending = IdentityReviewAnswer.objects.filter(
        job=job, section=section, status="queued"
    ).count()
    if isinstance(expected, bool) or expected != review.revision + pending:
        raise RevisionConflictError(review.revision, pending)
    if pending >= MAX_QUEUED:
        raise ConflictError("Wait until the queued answers are processed")
    answer = IdentityReviewAnswer.objects.create(
        id=request_id,
        job=job,
        section=section,
        requested_by=actor,
        sequence=expected,
        payload=fields,
    )
    schedule(target)
    return 202, {"queued": True, **answer_row(answer)}


@transaction.atomic
def prepare(store: Store, workspace: Workspace, run_id: str) -> dict:
    """Create (or refresh) the snapshot and crops of a run with a naming cache.

    A replay section can be named once its own child run completed, while
    later sections are still being analysed.

    Returns:
        The queued state.

    Raises:
        FileNotFoundError: The run was analysed without roster naming.

    """
    target = clip_target(workspace, run_id)
    if not finished(store, target) or not has_cache(store, workspace, target):
        raise FileNotFoundError("This clip run has no player-naming data")
    ClipIdentityReview.objects.get_or_create(job=target.job, section=target.section)
    schedule(target)
    return {"queued": True}


def finished(store: Store, target: Target) -> bool:
    """Whether the clip run, or the section's own child run, completed.

    Returns:
        True once the run's receipt (or job) says completed.

    """
    if not target.section:
        return target.job.status == "completed"
    marker = directory(store, target.run_id) / "run.json"
    with suppress(OSError, ValueError):
        return json.loads(marker.read_text(encoding="utf-8")).get("status") == (
            "completed"
        )
    return False


@transaction.atomic
def after_clip(job: AnalysisJob) -> None:
    """Prepare naming for a finished run that was analysed with a roster.

    A replay has no cache of its own: each section is prepared when a
    reviewer opens it (``read`` lists them).
    """
    options = job.payload.get("options") or {}
    if (
        job.status == "completed"
        and not job.payload.get("recording_end")
        and (options.get("match_identity") or {}).get("closed_set")
    ):
        ClipIdentityReview.objects.get_or_create(job=job, section="")
        schedule(Target(job))


def schedule(target: Target) -> None:
    """Coalesce re-solves per run on the vision worker that owns the runtime."""
    enqueue(TASK, target.run_id, args=[target.run_id], queue="vision")


def named_replay(job: AnalysisJob) -> bool:
    """Whether a clip job is a whole recording named against a roster.

    Returns:
        True for a replay whose options carry a closed-set roster.

    """
    options = job.payload.get("options") or {}
    return bool(
        job.payload.get("recording_end")
        and (options.get("match_identity") or {}).get("closed_set")
    )


def schedule_match_pass(job: AnalysisJob) -> None:
    """Queue one debounced match pass for a replay with a roster.

    Answers coalesce on one durable key that keeps the earliest due time, so
    ten quick answers run the pass once; an answer arriving while it runs
    queues exactly one more (``services/match_identity.py`` runs it).
    """
    if named_replay(job):
        enqueue(
            MATCH_TASK,
            str(job.pk),
            args=[str(job.pk)],
            queue="vision",
            due_at=timezone.now() + MATCH_DEBOUNCE,
        )


def engine_answer(target: Target, answer: IdentityReviewAnswer) -> dict:
    """Wrap a stored answer in the engine's versioned, owner-scoped envelope.

    Returns:
        The engine answer payload.

    """
    return {
        "version": 1,
        "namespace": target.job.payload["match_id"],
        "fingerprint": target.run_id,
        "expected_revision": answer.sequence,
        "request_id": str(answer.pk),
        **{key: value for key, value in answer.payload.items() if key != "name"},
    }


def hydrate(store: Store, workspace: Workspace, path: Path) -> None:
    """Restore one private run artifact for the worker if it was evicted."""
    name = path.relative_to(store.root).as_posix()
    if (
        not path.is_file()
        and StoredFile.objects.filter(workspace=workspace, relative_path=name).exists()
    ):
        store.media(name)


def process(run_id: str, runtime: IdentityReviewRuntime) -> None:
    """Apply queued answers, refresh the snapshot and crop the next questions.

    A replay section's answers also correct the replay's identity gallery: it
    is restored before the solve and published before answers are stored.
    """
    job_id, section = parse_run(run_id)
    job = AnalysisJob.objects.select_related("workspace").get(pk=job_id, kind="clip")
    target = Target(job, section)
    ClipIdentityReview.objects.get_or_create(job=job, section=section)
    answers = list(
        IdentityReviewAnswer.objects.filter(
            job=job, section=section, status="queued"
        ).order_by("sequence", "created_at")[:MAX_QUEUED]
    )
    request: dict[str, Any] = {
        "namespace": job.payload["match_id"],
        "fingerprint": target.run_id,
        "answers": [engine_answer(target, a) for a in answers],
    }
    try:
        with runtime.processing_store(job.workspace, None) as store:
            root = directory(store, target.run_id)
            hydrate(store, job.workspace, root / CACHE)
            hydrate(store, job.workspace, root / "run.json")
            gallery = directory(store, str(job.pk)) / GALLERY_FILE if section else None
            if gallery is not None:
                hydrate(store, job.workspace, gallery)
            record = json.loads((root / "run.json").read_text(encoding="utf-8"))
            chunks = [chunk["name"] for chunk in record.get("chunks", [])]
            for name in chunks:
                hydrate(store, job.workspace, root / name)
            video = store.recording(job.payload["match_id"])["video"]
            with store.video_source(video) as source:
                request["crops"] = {"video": source, "chunks": chunks}
                result = runtime.solve(store, target.run_id, request)
            if gallery is not None and gallery.is_file():
                store.publish_artifact(gallery.relative_to(store.root).as_posix())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        logger.exception("Identity review for clip %s failed", target.run_id)
        fail(target, answers)
        return
    with transaction.atomic():
        save(target, answers, result)
        if section and any(
            receipt.get("status") == "applied" for receipt in result.get("answers", [])
        ):
            # The replay's published names (playback without the panel, every
            # other section) change only when its match pass runs again.
            schedule_match_pass(job)
    if IdentityReviewAnswer.objects.filter(
        job=job, section=section, status="queued"
    ).exists():
        with transaction.atomic():
            schedule(target)


@transaction.atomic
def fail(target: Target, answers: list[IdentityReviewAnswer]) -> None:
    """Reject this batch; earlier snapshots stay readable."""
    review = ClipIdentityReview.objects.select_for_update().get(
        job=target.job, section=target.section
    )
    review.message = "Naming could not be updated. An operator can inspect the logs."
    review.status = "ready" if review.snapshot else "failed"
    review.save(update_fields=["message", "status", "updated_at"])
    IdentityReviewAnswer.objects.filter(
        pk__in=[a.pk for a in answers], status="queued"
    ).update(
        status="rejected",
        code="failed",
        message="The worker could not apply this answer",
        finished_at=timezone.now(),
    )


@transaction.atomic
def save(target: Target, answers: list[IdentityReviewAnswer], result: dict) -> None:
    """Store receipts and the new snapshot together, so revisions stay exact."""
    review = ClipIdentityReview.objects.select_for_update().get(
        job=target.job, section=target.section
    )
    snapshot = result["review"]
    if result.get("crop_error"):
        logger.warning(
            "Identity crops for clip %s: %s", target.run_id, result["crop_error"]
        )
        snapshot["crop_error"] = True
    review.snapshot, review.revision = snapshot, int(snapshot["revision"])
    review.status, review.message = "ready", ""
    review.save()
    receipts = {r["request_id"]: r for r in result.get("answers", [])}
    now = timezone.now()
    for answer in answers:
        receipt = receipts.get(str(answer.pk), {})
        answer.status = "applied" if receipt.get("status") == "applied" else "rejected"
        answer.code = str(receipt.get("code", ""))[:24]
        answer.message = str(receipt.get("message", ""))[:300]
        answer.revision = receipt.get("revision")
        answer.finished_at = now
        answer.save(
            update_fields=["status", "code", "message", "revision", "finished_at"]
        )
