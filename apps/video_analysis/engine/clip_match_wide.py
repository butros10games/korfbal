"""One roster identity over every section of a long recording.

A recording is analysed as bounded sections (``adapters/replay.py``), each its
own clip run with its own tracker. While a section runs it names players from
its own evidence. It also writes compact identity evidence beside its chunks:
its pure tracklets with up to 24 raw appearance samples each, their live
times, kit tag and shirt-number reads. When the last section is done, this
match pass solves the closed-set roster once for the whole recording:

- one appearance space is fitted over every section's tracklets, streaming one
  section at a time, so memory never holds more than one section's raw samples;
- the roster's kit orientation is one hypothesis for the whole recording,
  decided by the same evidence model as a clip run
  (``clip_number_anchors.orientation``) from every section's number reads and
  confirmations: the replay hands the first section's team colours to every
  later one, so kit tags mean the same in all sections (sides and zones play
  no part: korfbal teams change ends at half-time and attack/defence zones
  after every two goals, but keep their kits). While it is undecided no
  number or appearance names a kit-tagged tracklet;
- shirt-number anchors and human confirmations from any section train one
  classifier, so a number read in minute 50 names that player in minute 3;
- a reviewer's answers in any section's review cache (and answers passed to
  the pass) overrule numbers there, so a correction retracts a wrong number
  match-wide: the pass re-derives every name from raw evidence, never from
  names an earlier pass or section published;
- players a section's reviewer added to the roster (an unlinked recording, a
  substitute the line-up missed) are roster players of the whole pass
  (``recording_roster``);
- the same calibrated margins as a clip run decide what is published, under one
  footage decision for the recording (``footage``): the sections' camera tests
  vote by live tracklet weight; a descriptor calibrated on broadcast footage
  only names nobody by appearance in a fixed-camera recording, and in a
  broadcast recording not in its fixed-camera sections (a studio shot).

The result rewrites each section's frame links (``rename``); sections keep their
unnamed within-shot identities for players the match pass cannot name. Section
evidence is immutable and fingerprinted, so a failed or repeated section only
needs its own rerun before the (cheap, deterministic) match pass is repeated.

A repeated pass (after a reviewer's answer) renames the replay from every
section's own published links (``republish``), never from names an earlier
pass published, so a retracted name disappears everywhere. Run on a replay, the
command also renames that replay's published links in the store; in production
``manage.py republish_match_identity <replay>`` does the same through the
worker (restores the inputs from object storage, holds the worker's storage
lease and publishes the result), and a reviewer's answer queues it.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import hashlib
import importlib
from itertools import starmap
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from . import clip_discriminant
from .clip_closed_set import (
    MAX_DIMENSIONS,
    MAX_SAMPLES,
    RECIPE as CLOSED_SET_RECIPE,
    Confidence,
    Evidence,
    Player,
    ReviewSession,
    Scope,
    Settings,
    merge_additions,
)
from .clip_closed_set_calibration import BROADCAST_ONLY, calibrated
from .clip_match_evidence import EPSILON, MIN_CLASS_SAMPLES, MIN_CLASSES, SHRINKAGE
from .clip_match_identity import Anchor, Fragment, scope_link
from .clip_number_anchors import (
    SOURCE as NUMBER_SOURCE,
    KitRead,
    NumberPolicy,
    confident,
    kit_reads,
)
from .store import atomic_json
from .vision import digest


if TYPE_CHECKING:
    from collections.abc import Iterator

    from numpy.typing import NDArray


VERSION = 1
EVIDENCE = "identity-evidence.npz"
MANIFEST = "identity-evidence.json"
RESULT = "match-identity.json"
REVIEW = "identity-review.sqlite"
# A four-hour recording in 120 s sections, and a tracklet bound well above the
# ~30,000 pure tracklets of a 60-minute broadcast.
MAX_SECTIONS = 128
MAX_MATCH_TRACKLETS = 131_072
MATCH_DEADLINE = 3600.0
# Rows of raw samples handled per GPU/BLAS call while fitting.
FIT_CHUNK = 8192


def save(
    root: Path,
    section: dict[str, Any],
    pieces: list[Fragment],
    grouped: dict[str, list[dict[str, float]]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Write one section's bounded identity evidence beside its chunks.

    ``pieces`` are the section's pure tracklets (one raw view each, at most 24
    samples); ``grouped`` their readable shirt-number reads; ``context`` the
    descriptor checksum, team colours and number policy of the run.

    Returns:
        A receipt with the evidence checksum.

    """
    np = importlib.import_module("numpy")
    samples, owners, times, moments = [], [], [], []
    fragments = []
    for index, piece in enumerate(pieces):
        raw = next(iter(piece.raw.values()), None) if piece.raw else None
        if raw is not None and len(raw):
            values = np.asarray(raw, dtype=np.float16)[:MAX_SAMPLES]
            samples.append(values)
            owners.append(np.full(len(values), index, dtype=np.int32))
        ordered = sorted(piece.times)
        times.append(np.asarray(ordered, dtype=np.float64))
        moments.append(np.full(len(ordered), index, dtype=np.int32))
        fragments.append({
            "identity": piece.identity,
            "shot": piece.shot,
            "team": piece.team,
            "replay": piece.replay,
            "weight": piece.weight,
            "conflicted": piece.conflicted,
            "representative_track_id": piece.representative_track_id,
            "placement": list(piece.placement),
        })
    dimension = samples[0].shape[1] if samples else 0
    arrays = {
        "samples": np.concatenate(samples)
        if samples
        else np.zeros((0, dimension), np.float16),
        "sample_owner": np.concatenate(owners) if owners else np.zeros(0, np.int32),
        "times": np.concatenate(times) if times else np.zeros(0),
        "time_owner": np.concatenate(moments) if moments else np.zeros(0, np.int32),
    }
    staged = root / f".{EVIDENCE}.partial.npz"
    np.savez(
        staged,
        samples=arrays["samples"],
        sample_owner=arrays["sample_owner"],
        times=arrays["times"],
        time_owner=arrays["time_owner"],
    )
    staged.replace(root / EVIDENCE)
    checksum = digest(root / EVIDENCE)
    reads = {identity: values for identity, values in grouped.items() if values}
    atomic_json(
        root / MANIFEST,
        {
            "version": VERSION,
            "section": section,
            "context": context,
            "fragments": fragments,
            "reads": reads,
            "evidence_sha256": checksum,
            "samples": len(arrays["samples"]),
            "dimension": dimension,
        },
    )
    return {"status": "saved", "fragments": len(fragments), "sha256": checksum}


@dataclass
class Section:
    """One section's evidence on disk; raw samples load only when asked."""

    part: int
    root: Path
    manifest: dict[str, Any]

    @classmethod
    def open(cls, part: int, root: Path) -> Section:
        """Read a section's manifest and check its evidence checksum.

        Raises:
            ValueError: The evidence is missing, changed or of another version.

        """
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        if manifest.get("version") != VERSION:
            raise ValueError("Unsupported section identity evidence")
        if digest(root / EVIDENCE) != manifest["evidence_sha256"]:
            raise ValueError("Section identity evidence changed")
        return cls(part, root, manifest)

    def arrays(self) -> dict[str, NDArray[Any]]:
        """Load this section's raw samples and times."""
        np = importlib.import_module("numpy")
        with np.load(self.root / EVIDENCE, allow_pickle=False) as stored:
            return {key: stored[key] for key in stored.files}

    def scoped(self, identity: str) -> str:
        """Match-wide identity of one of this section's tracklets."""
        return f"p{self.part:04d}:{identity}"


def fingerprint(sections: list[Section], roster: list[Player]) -> str:
    """Identify the exact evidence and roster a match pass solved."""
    return hashlib.sha256(
        json.dumps(
            {
                "sections": [[s.part, s.manifest["evidence_sha256"]] for s in sections],
                "roster": [asdict(p) for p in roster],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def views(section: Section) -> Iterator[tuple[int, NDArray[Any]]]:
    """Read each fragment's raw samples of one section.

    Yields:
        The fragment index and its samples as float64.

    """
    np = importlib.import_module("numpy")
    arrays = section.arrays()
    samples, owner = arrays["samples"], arrays["sample_owner"]
    if not len(owner):
        return
    edges = np.flatnonzero(np.diff(owner)) + 1
    for chunk in np.split(np.arange(len(owner)), edges):
        yield int(owner[chunk[0]]), samples[chunk].astype(np.float64)


def match_space(
    sections: list[Section], *, dimensions: int = MAX_DIMENSIONS
) -> tuple[NDArray[Any], NDArray[Any]] | None:
    """Fit ``clip_match_evidence.fit`` over all sections, streaming sections.

    Pass one sums the samples for the centre; pass two accumulates every
    tracklet class's within-class scatter and keeps only class centres. The
    result is the same space ``fit`` would return for all tracklets at once.
    """
    np = importlib.import_module("numpy")
    total, count, dimension = None, 0, 0
    for section in sections:
        for _, values in views(section):
            total = values.sum(axis=0) if total is None else total + values.sum(axis=0)
            count += len(values)
            dimension = values.shape[1]
    if total is None or not count:
        return None
    centre = total / count
    within = np.zeros((dimension, dimension))
    centres, members = [], 0
    for section in sections:
        batch: list[NDArray[Any]] = []
        for _, values in views(section):
            if len(values) < MIN_CLASS_SAMPLES:
                continue
            mean = values.mean(axis=0)
            centres.append(mean - centre)
            batch.append(values - mean)
            members += len(values)
            if sum(len(b) for b in batch) >= FIT_CHUNK:
                within += scatter(np.concatenate(batch))
                batch = []
        if batch:
            within += scatter(np.concatenate(batch))
    if len(centres) < MIN_CLASSES:
        return None
    return centre, clip_discriminant.from_scatter(
        within / members,
        np.asarray(centres),
        clip_discriminant.Fit(dimensions, SHRINKAGE, EPSILON),
    )


def scatter(differences: NDArray[Any]) -> NDArray[Any]:
    """Sum of outer products of rows (GPU when the worker opts in and has room).

    Raises:
        RuntimeError: The GPU failed for another reason than memory.

    """
    if clip_discriminant.gpu() and clip_discriminant.room():
        torch = importlib.import_module("torch")
        try:
            x = torch.from_numpy(differences).to("cuda", dtype=torch.float64)
            return (x.T @ x).cpu().numpy()
        except RuntimeError as error:
            if not clip_discriminant.out_of_memory(error):
                raise
            torch.cuda.empty_cache()
    return differences.T @ differences


def project(
    section: Section,
    space: tuple[NDArray[Any], NDArray[Any]],
) -> dict[int, NDArray[Any]]:
    """Normalised match-space samples of a section's fragments."""
    np = importlib.import_module("numpy")
    centre, transform = space
    output = {}
    for index, values in views(section):
        projected = (values - centre) @ transform
        projected /= np.maximum(
            np.linalg.norm(projected, axis=1, keepdims=True), EPSILON
        )
        output[index] = projected.astype(np.float32)
    return output


def fragments(section: Section, projected: dict[int, NDArray[Any]]) -> list[Fragment]:
    """Rebuild a section's tracklets with match-wide IDs, in their kit tags."""
    np = importlib.import_module("numpy")
    arrays = section.arrays()
    times: dict[int, set[float]] = defaultdict(set)
    for moment, owner in zip(
        arrays["times"].tolist(), arrays["time_owner"].tolist(), strict=True
    ):
        times[owner].add(round(moment, 6))
    output = []
    for index, row in enumerate(section.manifest["fragments"]):
        samples = projected.get(index)
        mean = samples.mean(axis=0) if samples is not None else None
        output.append(
            Fragment(
                section.scoped(row["identity"]),
                f"p{section.part}:{row['shot']}",
                row["team"],
                times.get(index, set()),
                [mean / max(float(np.linalg.norm(mean)), EPSILON)]
                if mean is not None
                else [],
                replay=row["replay"],
                weight=row["weight"],
                conflicted=row["conflicted"],
                samples=list(samples) if samples is not None else [],
                representative_track_id=row["representative_track_id"],
                placement=tuple(row.get("placement", (0, 0))),
            )
        )
    return output


def scoped_reads(section: Section) -> dict[str, list[dict[str, float]]]:
    """Key a section's readable number reads by match-wide tracklet IDs."""
    return {
        section.scoped(identity): values
        for identity, values in section.manifest["reads"].items()
    }


def review_state(path: Path) -> dict[str, Any] | None:
    """Read a section review cache's answer state, or ``None`` without one."""
    if not path.is_file():
        return None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        row = db.execute("SELECT state FROM review WHERE id=1").fetchone()
    return json.loads(row[0]) if row else None


def review_revision(path: Path) -> int:
    """Return the revision of a section's stored answers (0 without a review)."""
    state = review_state(path)
    return int(state.get("revision", 0)) if state else 0


def section_answers(section: Section) -> tuple[list[dict[str, Any]], int]:
    """Read a reviewer's current answers from a section's own review cache.

    Confirmations and dismissals answered through the section's naming panel
    stay valid for the match pass; an undone answer is simply absent. Answers
    on views this section's evidence does not hold (its evidence was described
    with another descriptor) are counted, not used.

    Returns:
        The answers in ``Request.answers`` form and how many were skipped.

    """
    state = review_state(section.root / REVIEW)
    if state is None:
        return [], 0
    known = {fragment["identity"] for fragment in section.manifest["fragments"]}
    found: list[dict[str, Any]] = []
    skipped = 0
    for anchor in state.get("anchors", []):
        if anchor["identity"] in known:
            found.append({
                "section": section.part,
                "identity": anchor["identity"],
                "player_id": anchor["player_id"],
            })
        else:
            skipped += 1
    for identity, reason in (state.get("dismissals") or {}).items():
        if identity in known:
            found.append({
                "section": section.part,
                "identity": identity,
                "dismiss": reason,
            })
        else:
            skipped += 1
    return found, skipped


def section_roster(section: Section) -> list[Player]:
    """Read the roster a section's review ended with, its additions included.

    Raises:
        ValueError: A stored roster row is malformed.

    """
    path = section.root / REVIEW
    if not path.is_file():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        row = db.execute("SELECT roster FROM review WHERE id=1").fetchone()
    players = []
    for entry in json.loads(row[0]) if row else []:
        if (
            not isinstance(entry, dict)
            or set(entry) - {"player_id", "team", "number"}
            or entry.get("team") not in {"team_a", "team_b"}
            or not isinstance(entry.get("player_id"), str)
        ):
            raise ValueError("A section review holds a malformed roster player")
        players.append(Player(**entry))
    return players


def recording_roster(
    roster: list[Player], sections: list[Section]
) -> tuple[list[Player], dict[str, str], list[dict[str, Any]]]:
    """Add the players every section's reviewers added to the replay's roster.

    The recipe's roster predates the review: a reviewer adds a substitute the
    line-up missed, or fills an unlinked recording's empty roster ("Speler
    toevoegen"). Those players are the recording's players, with the policy of
    ``clip_closed_set.merge_additions`` (the same number on the same side is
    one player).

    Returns:
        The match roster, aliases (addition ID -> roster ID) and the receipt of
        added players with the section that added them.

    """
    merged, aliases, added_from = list(roster), {}, []
    for section in sections:
        merged, found, added = merge_additions(merged, section_roster(section))
        aliases.update(found)
        added_from.extend({**asdict(p), "section": section.part} for p in added)
    return merged, aliases, added_from


def answer_anchors(
    answers: list[dict[str, Any]],
    roster: list[Player],
    parts: set[int],
    aliases: dict[str, str] | None = None,
) -> tuple[list[Anchor], dict[str, str], int]:
    """Human confirmations and dismissals of any section, in match-wide IDs.

    A later answer on the same view replaces an earlier one. ``aliases`` map a
    player a reviewer added to the roster player wearing the same number.
    Confirmations of a player outside the match roster cannot name a
    match-wide class and are counted instead.

    Returns:
        The confirmations, the dismissals and how many answers named a player
        outside the roster.

    Raises:
        ValueError: An answer names an unknown section.

    """
    players = {p.player_id: p for p in roster}
    confirmed: dict[str, Anchor] = {}
    dismissed: dict[str, str] = {}
    outside = 0
    for row in answers:
        part = int(row["section"])
        if part not in parts:
            raise ValueError("An answer names an unknown section")
        identity = f"p{part:04d}:{row['identity']}"
        confirmed.pop(identity, None)
        dismissed.pop(identity, None)
        if row.get("dismiss"):
            dismissed[identity] = str(row["dismiss"])
            continue
        player = players.get((aliases or {}).get(row["player_id"], row["player_id"]))
        if player is None:
            outside += 1
            continue
        confirmed[identity] = Anchor(
            identity, player.player_id, player.team, player.number
        )
    return list(confirmed.values()), dismissed, outside


@dataclass
class MatchIdentity:
    """The match pass: evidence, fitted space and one roster review session."""

    sections: list[Section]
    roster: list[Player]
    session: ReviewSession
    receipt: dict[str, Any]


def shared_descriptor(
    sections: list[Section],
) -> tuple[list[Section], list[dict[str, Any]]]:
    """Keep the sections described with the match's main appearance model.

    One appearance space needs one descriptor. The model that described most
    tracklets wins; the others are reported and keep their section names.

    Raises:
        ValueError: No section has any tracklet.

    """
    weight: Counter[str] = Counter()
    for section in sections:
        weight[section.manifest["context"]["descriptor_sha256"]] += len(
            section.manifest["fragments"]
        )
    if not weight:
        raise ValueError("No section has identity evidence")
    main = weight.most_common(1)[0][0]
    kept = [s for s in sections if s.manifest["context"]["descriptor_sha256"] == main]
    excluded = [
        {
            "section": s.part,
            "descriptor_sha256": s.manifest["context"]["descriptor_sha256"],
        }
        for s in sections
        if s not in kept
    ]
    return kept, excluded


def footage(sections: list[Section], checksum: str) -> dict[str, Any]:
    """Decide the recording's footage once, and where its calibration applies.

    Every section's linker judged its own camera fixed (one tripod: a club
    recording) or operated (broadcast). A recording is one camera set-up, so
    the sections vote by live tracklet weight and a tie is fixed, the side that
    names fewer players. Sections judged otherwise are the minority: a studio
    shot in a broadcast, or a club section misjudged as operated. A descriptor
    whose calibration was measured on broadcast footage only (the korfbal
    adapter: about 85% precise on the club development clip) is calibrated
    nowhere in a fixed recording and not on the fixed sections of a broadcast
    one; other descriptors' calibrations cover all footage.

    Returns:
        The decision for the receipt: the recording's footage, its fixed and
        operated sections, the calibration's domain and the sections whose
        views publish no appearance names.

    """
    weight = {True: 0.0, False: 0.0}
    fixed_parts, operated_parts = [], []
    for section in sections:
        fixed = bool(section.manifest["context"].get("fixed_camera", False))
        (fixed_parts if fixed else operated_parts).append(section.part)
        weight[fixed] += sum(
            float(row.get("weight") or 0)
            for row in section.manifest["fragments"]
            if not row.get("replay")
        )
    recording = "fixed" if weight[True] >= weight[False] else "broadcast"
    domain = "broadcast" if checksum in BROADCAST_ONLY else "all_footage"
    uncalibrated = (
        [s.part for s in sections]
        if domain == "broadcast" and recording == "fixed"
        else fixed_parts
        if domain == "broadcast"
        else []
    )
    return {
        "recording": recording,
        "fixed_sections": fixed_parts,
        "operated_sections": operated_parts,
        "calibration_domain": domain,
        "uncalibrated_sections": uncalibrated,
    }


def stored_answers(sections: list[Section]) -> tuple[list[dict[str, Any]], int]:
    """Collect every section review's current answers, in part order.

    Returns:
        The answers and how many named views the evidence does not hold.

    """
    stored: list[dict[str, Any]] = []
    skipped = 0
    for section in sections:
        answers, missing = section_answers(section)
        stored.extend(answers)
        skipped += missing
    return stored, skipped


def match_settings(
    sections: list[Section], checksum: str, workers: int | None
) -> tuple[Settings, dict[str, Any]]:
    """Build the pass's solver settings under the recording's footage decision.

    One footage decision for the recording (``footage``): never the broadcast
    operating point on fixed-camera footage because one section was operated.

    Returns:
        The settings and the footage decision for the receipt.

    """
    decided = footage(sections, checksum)
    confidence: Confidence | None = calibrated(
        checksum, "tracklets", fixed_camera=decided["recording"] == "fixed"
    )
    if confidence is None:
        decided["uncalibrated_sections"] = [s.part for s in sections]
    settings = Settings(
        f"{checksum}:{CLOSED_SET_RECIPE}:tracklets",
        confidence,
        max_tracklets=MAX_MATCH_TRACKLETS,
        deadline=MATCH_DEADLINE,
        workers=workers or max(1, min(16, (os.cpu_count() or 2) - 1)),
        uncalibrated=tuple(
            f"p{part:04d}:" for part in decided["uncalibrated_sections"]
        ),
    )
    return settings, decided


@dataclass(frozen=True)
class Request:
    """Whose recording this is, reviewer answers from any section, and workers.

    ``answers`` come after (and win over) the answers stored in the sections'
    own review caches, which are read unless ``section_reviews`` is false.
    """

    namespace: str
    answers: tuple[dict[str, Any], ...] = ()
    workers: int | None = None
    section_reviews: bool = True


def solve_match(
    sections: list[Section],
    roster: list[Player],
    request: Request,
) -> MatchIdentity:
    """Solve the whole recording's roster once from every section's evidence.

    Raises:
        ValueError: Sections disagree on their descriptor or exceed the bounds.

    """
    started = time.monotonic()
    if not sections or len(sections) > MAX_SECTIONS:
        raise ValueError("A match pass needs 1 to 128 sections")
    # Players section reviewers added are roster players in every section.
    roster, aliases, additions = (
        recording_roster(roster, sections)
        if request.section_reviews
        else (roster, {}, [])
    )
    sections, excluded = shared_descriptor(sections)
    checksum = sections[0].manifest["context"]["descriptor_sha256"]
    timings: dict[str, float] = {}
    space = match_space(sections)
    timings["fit"] = time.monotonic() - started
    if space is None:
        raise ValueError("Too little appearance evidence for a match space")
    pieces: dict[int, list[Fragment]] = {}
    for section in sections:
        pieces[section.part] = fragments(section, project(section, space))
    timings["project"] = time.monotonic() - started - timings["fit"]
    if sum(len(v) for v in pieces.values()) > MAX_MATCH_TRACKLETS:
        raise ValueError("The recording exceeds the match tracklet bound")
    policy = NumberPolicy.from_receipt(sections[0].manifest["context"].get("numbers"))
    # Reads stay kit evidence: the session decides the orientation for the whole
    # recording and names nobody by number while it is undecided.
    reads: list[KitRead] = []
    confident_per_section: dict[int, int] = {}
    for section in sections:
        found = (
            kit_reads(confident(pieces[section.part], scoped_reads(section), policy))
            if policy is not None
            else []
        )
        confident_per_section[section.part] = len(found)
        reads.extend(found)
    everything = [p for section in sections for p in pieces[section.part]]
    stored, skipped = stored_answers(sections) if request.section_reviews else ([], 0)
    humans, dismissed, outside = answer_anchors(
        [*stored, *request.answers], roster, {s.part for s in sections}, aliases
    )
    settings, decided = match_settings(sections, checksum, request.workers)
    confidence = settings.calibration
    session = ReviewSession(
        Scope(request.namespace, fingerprint(sections, roster)),
        everything,
        roster,
        settings,
        Evidence(
            anchors=tuple(humans),
            numbers=tuple(reads),
            dismissals=tuple(dismissed.items()),
        ),
    )
    numbered: Counter[int] = Counter()
    named_players: dict[int, set[str]] = defaultdict(set)
    for row in session.result.get("assignments", []):
        if row.get("status") == "anchored" and row.get("source") == NUMBER_SOURCE:
            part = int(row["identity"].split(":", 1)[0].removeprefix("p"))
            numbered[part] += 1
            named_players[part].add(row["player_id"])
    anchor_receipts = [
        {
            "section": section.part,
            "confident_tracklets": confident_per_section[section.part],
            "accepted": numbered[section.part],
            "players": len(named_players[section.part]),
        }
        for section in sections
    ]
    timings["solve"] = time.monotonic() - started - timings["fit"] - timings["project"]
    receipt = {
        "version": VERSION,
        "status": session.result.get("status"),
        "sections": len(sections),
        # Sections described with another model (e.g. a studio shot the
        # linker judged a fixed camera) keep their own section names.
        "excluded_sections": excluded,
        "tracklets": len(everything),
        "descriptor_sha256": checksum,
        "calibrated": confidence is not None,
        "footage": decided,
        "kit_orientation": session.result.get("orientation"),
        "number_anchors": {
            "accepted": sum(numbered.values()),
            "players": len(set().union(*named_players.values())),
            "sections": anchor_receipts,
        },
        # Ambiguous reads appearance contradicted, and players misreads explain.
        "number_checks": session.result.get("number_checks"),
        "confirmations": len(humans),
        "dismissals": len(dismissed),
        "answers_outside_roster": outside,
        "roster_additions": additions,
        "roster_aliases": aliases,
        "answers_on_unknown_views": skipped,
        # The revision of every section's answers this pass solved with: a
        # later answer makes the published names stale until the next pass.
        "section_reviews": {
            f"part-{s.part:04d}": review_revision(s.root / REVIEW) for s in sections
        }
        if request.section_reviews
        else {},
        "timings_seconds": {k: round(v, 2) for k, v in timings.items()},
    }
    return MatchIdentity(sections, roster, session, receipt)


def rename(report: dict[str, Any], part: int, result: dict[str, Any]) -> dict:
    """Rewrite one section's frame links with the match-wide names.

    Mirrors ``clip_match_evidence.compose_tracklets``: a named tracklet shows
    its player; an unnamed one keeps its within-shot linked identity.
    """
    assignments = {a["identity"]: a for a in result.get("assignments", [])}
    links = []
    for link in report.get("frame_links", []):
        identity = link.get("fragment_identity")
        assignment = assignments.get(f"p{part:04d}:{identity}") if identity else None
        if assignment is None:
            links.append(link)
            continue
        player = assignment["player_id"]
        unnamed = link.get("unnamed_track_id", link["to_track_id"])
        links.append({
            **link,
            "to_track_id": player or unnamed,
            "display_id": assignment["display_id"]
            if player
            else link.get("unnamed_display_id", link.get("display_id")),
            "source": "match_identity",
            "name_origin": assignment.get("origin") if player else None,
            # What named it: a shirt number, calibrated appearance or a person.
            "name_source": assignment.get("source") if player else None,
        })
    return {**report, "frame_links": links}


def apply_to_replay(refinement: dict[str, Any], result: dict[str, Any]) -> dict:
    """Rename a replay's merged, section-scoped frame links with match-wide names.

    The replay keeps every section's links with their ``processing_section``
    and section-scoped IDs (``adapters/replay.py``); unnamed tracklets keep
    their section-scoped within-shot identity. Pure Python: the worker applies
    it without NumPy.
    """
    assignments = {a["identity"]: a for a in result.get("assignments", [])}
    links = []
    for link in refinement.get("frame_links", []):
        part, identity = link.get("processing_section"), link.get("fragment_identity")
        assignment = (
            assignments.get(f"p{int(part):04d}:{identity}")
            if part is not None and identity
            else None
        )
        if assignment is None:
            links.append(link)
            continue
        player = assignment["player_id"]
        # Replay links are section-scoped already (``scope_link``), including
        # the within-shot identity an unnamed tracklet falls back to.
        unnamed = link.get("unnamed_track_id", link["to_track_id"])
        links.append({
            **link,
            "to_track_id": player or unnamed,
            "display_id": assignment["display_id"]
            if player
            else link.get("unnamed_display_id", link.get("display_id")),
            "source": "match_identity",
            "name_origin": assignment.get("origin") if player else None,
            "name_source": assignment.get("source") if player else None,
        })
    receipt = {key: value for key, value in result.items() if key != "assignments"}
    return {**refinement, "frame_links": links, "match_wide": receipt}


# What a replay's own receipt keeps of a match pass's result.
WIDE_RECEIPT = (
    "status",
    "sections",
    "tracklets",
    "number_anchors",
    "confirmations",
    "dismissals",
    "roster_additions",
    "section_reviews",
    "calibrated",
    "footage",
)


def wide_receipt(result: dict[str, Any]) -> dict[str, Any]:
    """Summarise a match pass for the replay's receipt."""
    return {key: result[key] for key in WIDE_RECEIPT if key in result}


def section_links(children: list[dict[str, Any]]) -> dict[str, list[dict]]:
    """Merge every section's own published links as the replay merged them.

    ``children`` are the sections' final receipts in part order; the IDs are
    scoped like ``adapters/replay.py`` scopes them while sections publish.
    """
    merged: dict[str, list[dict]] = {"links": [], "frame_links": []}
    for part, child in enumerate(children):
        refinement = child.get("identity_refinement") or {}
        for field, links in merged.items():
            links.extend(
                scope_link(link, part, refinement.get("match_identity"))
                for link in refinement.get(field, [])
            )
    return merged


def republish(
    record: dict[str, Any], children: list[dict[str, Any]], result: dict[str, Any]
) -> dict[str, Any]:
    """Rename a replay's published links from its sections' own links.

    A repeated pass must not start from names an earlier pass published: a
    name a reviewer's correction retracted would otherwise survive in links
    the new result leaves unnamed.

    Returns:
        The replay's new ``identity_refinement``.

    """
    refinement = {
        **record.get("identity_refinement", {}),
        **section_links(children),
    }
    return apply_to_replay(refinement, result)


def compact(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep the published assignments only (what the replay overlay needs)."""
    return [
        {
            key: row.get(key)
            for key in (
                "identity",
                "player_id",
                "display_id",
                "status",
                "origin",
                "source",
                "margin",
                "candidate_player_id",
                "confidence",
            )
        }
        for row in result.get("assignments", [])
    ]


def section_roots(store_root: Path, replay: dict[str, Any]) -> list[tuple[int, Path]]:
    """List the completed child runs of a replay, in order."""
    clips = store_root / "vision" / "clips"
    return [
        (part, clips / f"{replay['id']}-part-{part:04d}")
        for part in range(int(replay.get("completed_parts", 0)))
    ]


def replay_sections(
    store_root: Path, replay: dict[str, Any], inputs: dict[str, Any] | None = None
) -> list[Section]:
    """Open every completed section's evidence; never solve on a partial set.

    A section whose child receipt says it saved identity evidence must still
    have exactly that evidence; sections that saved none (nothing to describe)
    are skipped. ``inputs`` are what the worker staged from object storage
    (``adapters/match_identity.py``): every section, its evidence checksum and
    the revision of its reviewer's answers, which the cache must still hold.

    Raises:
        ValueError: A section's evidence or answers are missing or changed.

    """
    roots = section_roots(store_root, replay)
    staged = {int(row["part"]): row for row in (inputs or {}).get("sections", [])}
    if inputs is not None and sorted(staged) != [part for part, _ in roots]:
        raise ValueError("The staged inputs do not cover the replay's sections")
    sections = []
    for part, root in roots:
        label = f"part-{part:04d}"
        if inputs is not None:
            expected = staged[part].get("evidence_sha256")
        else:
            if not (root / "run.json").is_file():
                raise ValueError(f"Section {label} is unavailable")
            saved = (
                json.loads((root / "run.json").read_text(encoding="utf-8")).get(
                    "match_evidence"
                )
                or {}
            )
            expected = saved.get("sha256") if saved.get("status") == "saved" else None
        if expected is None:
            continue
        if not ((root / MANIFEST).is_file() and (root / EVIDENCE).is_file()):
            raise ValueError(f"Section {label}'s identity evidence is unavailable")
        section = Section.open(part, root)
        if section.manifest["evidence_sha256"] != expected:
            raise ValueError(f"Section {label}'s identity evidence changed")
        if inputs is not None and review_revision(root / REVIEW) != int(
            staged[part].get("review_revision", 0)
        ):
            raise ValueError(f"Section {label}'s answers changed after staging")
        sections.append(section)
    return sections


def main() -> None:
    """Run the match pass for a finished replay (or listed section runs).

    Raises:
        SystemExit: The replay has no match roster.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    parser.add_argument("replay", help="Replay ID, or an output name with --runs")
    parser.add_argument("--runs", nargs="*", help="Section run IDs in part order")
    parser.add_argument("--roster", type=Path, help="Roster JSON with --runs")
    parser.add_argument("--namespace", help="Recording namespace with --runs")
    parser.add_argument("--answers", type=Path)
    parser.add_argument(
        "--inputs", type=Path, help="Staged inputs to verify (the worker writes it)"
    )
    parser.add_argument("--workers", type=int)
    args = parser.parse_args()
    clips = args.store / "vision" / "clips"
    if args.runs:
        roster = [Player(**p) for p in json.loads(args.roster.read_text())]
        roots = [(part, clips / run) for part, run in enumerate(args.runs)]
        namespace, target = args.namespace or args.replay, args.store / args.replay
        sections = list(starmap(Section.open, roots))
    else:
        replay = json.loads((clips / args.replay / "run.json").read_text())
        closed = (replay["recipe"]["options"].get("match_identity") or {}).get(
            "closed_set"
        )
        if not closed:
            raise SystemExit("This replay has no match roster")
        end = float(replay["recipe"].get("recording_end") or 0)
        if float(replay.get("next_start", 0)) < end - 1e-6:
            raise SystemExit("This replay still has sections to analyse")
        roster = [Player(**p) for p in closed["roster"]]
        inputs = json.loads(args.inputs.read_text()) if args.inputs else None
        sections = replay_sections(args.store, replay, inputs)
        namespace = str(replay["recipe"]["match_id"])
        target = clips / args.replay / RESULT
    answers = json.loads(args.answers.read_text()) if args.answers else []
    match = solve_match(
        sections, roster, Request(namespace, tuple(answers), args.workers)
    )
    result = {**match.receipt, "assignments": compact(match.session.result)}
    atomic_json(target, result)
    if not args.runs:
        # Rename the replay's published links too: rerunning the pass is how a
        # correction reaches playback and every other section.
        marker = clips / args.replay / "run.json"
        replay = json.loads(marker.read_text(encoding="utf-8"))
        children = [
            json.loads((path / "run.json").read_text(encoding="utf-8"))
            for _, path in section_roots(args.store, replay)
        ]
        replay["identity_refinement"] = republish(replay, children, result)
        replay["match_identity_wide"] = wide_receipt(result)
        atomic_json(marker, replay)
    print(json.dumps(match.receipt))


if __name__ == "__main__":
    main()
