"""Closed-set match identification and incremental, revision-fenced review.

Tracklets remain immutable. A class-balanced supervised ridge head is fitted on
confirmed crop descriptors in the per-match discriminant space. Every live time
window is a Hungarian assignment with private unknown slots. Whole-tracklet
consistency and a final exclusivity pass protect against contradictory windows.
Margins are evidence scores; only a recipe-matched empirical calibrator supplies
probabilities. Without calibration only human confirmations are published.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import importlib
import json
from operator import itemgetter
from threading import RLock
import time
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from numpy.typing import NDArray

from .clip_live_play import in_play
from .clip_match_evidence import fit_fragments
from .clip_match_gallery import RecordingGallery
from .clip_match_identity import (
    TEAMS,
    Anchor,
    Fragment,
    nearest_gap,
    overlap,
    validate_anchors,
)
from .clip_number_anchors import (
    CONFIRMATION,
    NUMBER,
    SOURCE as NUMBER_SOURCE,
    KitRead,
    Orientation,
    Sighting,
    absent,
    named,
    orientation,
    respecting,
    roster_side,
    sightings,
)
from .clip_section_identity import (
    AUTOMATIC_SOURCE as AUTOMATIC_GALLERY_SOURCE,
    SOURCE as GALLERY_SOURCE,
)


MAX_TRACKLETS = 4096
MAX_ROSTER = 64
MAX_SAMPLES = 24
MAX_DIMENSIONS = 128
SAMPLE_AXES = 2
MAX_REQUEST_ID = 128
MAX_RECEIPTS = 256
# Kit-orientation sightings of a replay's other sections (views, not frames).
MAX_LEDGER = 32 * MAX_TRACKLETS
AUTOMATIC_THRESHOLD = 0.995
RIDGE = 0.08
CONSENSUS = 0.85
SELF_TRAIN_MARGIN = 0.15
MIN_SAMPLES = 3
NEIGHBOUR_SECONDS = 3
ROUNDS = 2
# Folds of the number-anchor cross-check (leave-one-out below this many anchors).
FOLDS = 10
# Views a class needs before its raw classifier score may judge a read.
MIN_JUDGE_VIEWS = 3
DEADLINE = 15.0
# Below this many live windows a process pool costs more than it saves.
PARALLEL_WINDOWS = 2000
FORBIDDEN = -1e9
ANCHOR_SCORE = 1e6
RECIPE = "closed-set:ridge128:hungarian:team:consensus85:neighbour-selftrain2:v2"
# Frozen research proposal cutoff (raw margin, not a probability). Review tools
# show such candidates as automatic suggestions; they are never published names.
PROPOSAL_MARGIN = 0.03
DISMISSALS = frozenset({"not_player", "referee", "unclear"})
# Views a reviewer marked as nobody on the roster never take a roster slot.
EXCLUDING = frozenset({"not_player", "referee"})
OTHER_KIT = {"team_a": "team_b", "team_b": "team_a"}
# Players an earlier section of the recording named, loaded from its gallery.
CARRIED_SOURCES = frozenset({GALLERY_SOURCE, AUTOMATIC_GALLERY_SOURCE})
# Anchors the engine set without a person: shown and counted as automatic.
AUTOMATIC_SOURCES = frozenset({NUMBER_SOURCE, AUTOMATIC_GALLERY_SOURCE})
STATE_VERSION = 3
MAX_PLAYER_ID = 64
MAX_SHIRT = 99
ANSWER_FIELDS = {
    "confirm": frozenset({"identity", "player_id"}),
    "dismiss": frozenset({"identity", "reason"}),
    "remove": frozenset({"identity"}),
    "add_player": frozenset({"player"}),
}


@dataclass(frozen=True)
class Player:
    """A match-unique roster member; substitutions retain distinct player IDs."""

    player_id: str
    team: str
    number: str | None = None


@dataclass(frozen=True)
class Confidence:
    """Independent empirical margin/precision bins for one descriptor recipe."""

    provenance: str
    descriptor_recipe: str
    bins: tuple[tuple[float, float], ...]

    def probability(self, margin: float) -> float:
        """Return the empirical precision of the highest reached margin bin."""
        result = 0.0
        for boundary, precision in self.bins:
            if margin >= boundary:
                result = precision
        return result


@dataclass(frozen=True)
class Settings:
    """Recipe-owned confidence policy for a cached match classifier.

    The bounds default to one clip run. A whole-recording session
    (``clip_match_wide``) raises them and solves its live windows in parallel
    worker processes; the classifier recipe is the same. ``uncalibrated``
    lists view-ID prefixes (a recording's sections) whose footage the
    calibration was not measured on: their views publish no appearance names
    and their number reads are not judged by appearance.
    """

    descriptor_recipe: str
    calibration: Confidence | None = None
    minimum_confidence: float = 0.98
    max_tracklets: int = MAX_TRACKLETS
    deadline: float = DEADLINE
    workers: int = 1
    uncalibrated: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """What names players before any answer, in roster sides or kit tags.

    ``anchors`` are verified confirmations and players carried from earlier
    sections (roster sides); ``numbers`` are confident shirt-number reads (kit
    tags); ``kits`` states that the roster sides are the kit tags. ``ledger``
    holds the kit-orientation sightings of a replay's other sections, and
    ``section`` names this one, whose own sightings the session derives.
    ``dismissals`` are views a reviewer already dismissed (view, reason), so a
    match pass that collects every section's answers solves once.
    """

    anchors: tuple[Anchor, ...] = ()
    numbers: tuple[KitRead, ...] = ()
    kits: bool = False
    ledger: tuple[Sighting, ...] = ()
    section: str = ""
    dismissals: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Scope:
    """Server-owned identity of the exact recording/run evidence under review."""

    namespace: str
    fingerprint: str


def samples(piece: Fragment) -> NDArray[Any]:
    """Use crop samples where retained; gallery prototypes remain compatible."""
    np = importlib.import_module("numpy")
    values = np.asarray(piece.samples or piece.vectors, dtype=np.float64)
    if len(values) > MAX_SAMPLES:
        values = values[np.linspace(0, len(values) - 1, MAX_SAMPLES).astype(int)]
    return values


def unit(values: NDArray[Any]) -> NDArray[Any]:
    """Normalize nonempty projected descriptor vectors."""
    np = importlib.import_module("numpy")
    return values / np.maximum(np.linalg.norm(values, axis=-1, keepdims=True), 1e-9)


def windows(pieces: list[Fragment]) -> list[tuple[int, ...]]:
    """Build unique simultaneous live-body sets without relying on team guesses."""
    active: dict[float, list[int]] = defaultdict(list)
    for index, piece in enumerate(pieces):
        if not piece.replay:
            for moment in piece.times:
                active[moment].append(index)
    result = {tuple(indices) for indices in active.values()}
    # Retained recording gallery views have no live occupancy, but need proposals.
    result.update((i,) for i, p in enumerate(pieces) if p.replay or not p.times)
    return sorted(result)


def checked_anchors(
    pieces: list[Fragment], roster: list[Player], anchors: list[Anchor]
) -> dict[str, Anchor]:
    """Validate roster ownership and all live-time conflicts before fitting.

    Raises:
        ValueError: A name is absent from the roster or contradicts verified evidence.

    """
    players = {p.player_id: p for p in roster}
    if (
        (anchors and not roster)
        or len(players) != len(roster)
        or len(roster) > MAX_ROSTER
    ):
        raise ValueError("Choose a unique bounded match roster")
    for anchor in anchors:
        player = players.get(anchor.player_id)
        if player is None or (anchor.team, anchor.number) != (
            player.team,
            player.number,
        ):
            raise ValueError("Confirmation must match a member of this match roster")
    return validate_anchors(pieces, anchors)


def fit_scores(
    values: list[NDArray[Any]], means: NDArray[Any], known: dict[int, int], players: int
) -> NDArray[Any]:
    """Fit a class-balanced ridge classifier from confirmed and safe pseudo labels."""
    np = importlib.import_module("numpy")
    usable = {i: p for i, p in known.items() if len(values[i])}
    if not usable:
        return np.zeros((len(values), players))
    counts = Counter(usable.values())
    x = np.concatenate([values[i] for i in usable])
    y = np.concatenate([
        np.tile(np.eye(players)[p], (len(values[i]), 1)) for i, p in usable.items()
    ])
    weights = np.concatenate([
        np.full(len(values[i]), 1 / (len(values[i]) * counts[p]))
        for i, p in usable.items()
    ])
    transform = np.linalg.solve(
        x.T @ (x * weights[:, None]) + RIDGE * np.eye(x.shape[1]),
        x.T @ (y * weights[:, None]),
    )
    return means @ transform


def add_scores(
    scores: NDArray[Any],
    pieces: list[Fragment],
    players: list[str],
    roster: list[Player],
    confirmed: dict[int, int],
) -> None:
    """Use independent calibrated number mass and verified court as soft evidence."""
    by_player = {p.player_id: p for p in roster}
    for index, piece in enumerate(pieces):
        episodes = {n.episode: n for n in piece.numbers if n.calibrated}
        for column, name in enumerate(players):
            player = by_player[name]
            if (
                index not in confirmed
                and piece.team in {"team_a", "team_b"}
                and piece.team != player.team
            ):
                scores[index, column] = FORBIDDEN
                continue
            if player.number is not None and piece.team == player.team and episodes:
                scores[index, column] += (
                    0.25
                    * sum(
                        n.probabilities.get(player.number, 0) * n.readability
                        for n in episodes.values()
                    )
                    / len(episodes)
                )
            penalty = court_penalty(
                piece, [pieces[i] for i, p in confirmed.items() if p == column]
            )
            scores[index, column] -= penalty


def court_penalty(piece: Fragment, anchors: list[Fragment]) -> float:
    """Apply a weak reachability prior only in one explicitly verified court frame."""
    if not piece.reference_verified or piece.court is None or not piece.times:
        return 0.0
    import_math = importlib.import_module("math")
    candidates = []
    for anchor in anchors:
        if (
            anchor.reference_verified
            and anchor.reference == piece.reference
            and anchor.court
            and anchor.times
        ):
            gap = nearest_gap(piece.times, anchor.times)
            excess = max(
                0.0, import_math.dist(piece.court, anchor.court) - 7.5 * gap - 3.0
            )
            candidates.append(min(0.1, excess * 0.001))
    return min(candidates, default=0.0)


def consistent_assignment(
    scores: NDArray[Any],
    occupancy: list[tuple[int, ...]],
    confirmed: dict[int, int],
    pieces: list[Fragment],
    stopped: Callable[[], bool],
) -> tuple[list[int], list[float]] | None:
    """Compare best/second consistent assignments in each live time window."""
    return windowed_assignment(scores, Windows(occupancy), confirmed, pieces, stopped)


@dataclass(frozen=True)
class Windows:
    """Unique live occupancy sets and how many processes may solve them."""

    occupancy: list[tuple[int, ...]]
    workers: int = 1


def windowed_assignment(
    scores: NDArray[Any],
    windows: Windows,
    confirmed: dict[int, int],
    pieces: list[Fragment],
    stopped: Callable[[], bool],
) -> tuple[list[int], list[float]] | None:
    """``consistent_assignment``, optionally solving windows in parallel."""
    count = scores.shape[0]
    votes: list[Counter] = [Counter() for _ in range(count)]
    margins: list[dict[int, list[float]]] = [defaultdict(list) for _ in range(count)]
    scores = scores.copy()
    for index, player in confirmed.items():
        scores[index] = FORBIDDEN
        scores[index, player] = ANCHOR_SCORE
    if windows.workers > 1 and len(windows.occupancy) >= PARALLEL_WINDOWS:
        rows = pooled_votes(scores, windows, stopped)
    else:
        rows = []
        for window in windows.occupancy:
            if stopped():
                return None
            rows.extend(window_votes(scores, [window]))
    if rows is None:
        return None
    for index, player, gap in rows:
        votes[index][player] += 1
        if gap is not None:
            margins[index][player].append(gap)
    return settle(votes, margins, pieces, windows.occupancy, confirmed)


def pooled_votes(
    scores: NDArray[Any], windows: Windows, stopped: Callable[[], bool]
) -> list[tuple[int, int, float | None]] | None:
    """Solve independent windows in worker processes, in window order."""
    futures = importlib.import_module("concurrent.futures")
    context = importlib.import_module("multiprocessing").get_context("fork")
    occupancy = windows.occupancy
    size = max(1, len(occupancy) // (windows.workers * 4))
    chunks = [occupancy[i : i + size] for i in range(0, len(occupancy), size)]
    rows: list[tuple[int, int, float | None]] = []
    with futures.ProcessPoolExecutor(windows.workers, mp_context=context) as pool:
        jobs = [pool.submit(window_votes, scores, chunk) for chunk in chunks]
        for job in jobs:
            if stopped():
                for pending in jobs:
                    pending.cancel()
                return None
            rows.extend(job.result())
    return rows


def window_votes(
    scores: NDArray[Any], windows: list[tuple[int, ...]]
) -> list[tuple[int, int, float | None]]:
    """Each row's assigned player in its windows and its gap to the runner-up."""
    np = importlib.import_module("numpy")
    hungarian = importlib.import_module("scipy.optimize").linear_sum_assignment
    players = scores.shape[1]
    output: list[tuple[int, int, float | None]] = []
    for window in windows:
        matrix = np.concatenate(
            [scores[list(window)], np.zeros((len(window), len(window)))], axis=1
        )
        rows, cols = hungarian(-matrix)
        best = float(matrix[rows, cols].sum())
        for row, column in zip(rows, cols, strict=True):
            index = window[int(row)]
            player = int(column) if column < players else -1
            gap = None
            if player >= 0:
                alternative = matrix.copy()
                alternative[row, column] = FORBIDDEN
                other_rows, other_cols = hungarian(-alternative)
                # A swap may change two rows; use the conservative per-row gap.
                gap = max(
                    0.0, (best - float(alternative[other_rows, other_cols].sum())) / 2
                )
            output.append((index, player, gap))
    return output


def settle(
    votes: list[Counter],
    margins: list[dict[int, list[float]]],
    pieces: list[Fragment],
    occupancy: list[tuple[int, ...]],
    confirmed: dict[int, int],
) -> tuple[list[int], list[float]]:
    """Require whole-tracklet consensus and retain only globally exclusive labels."""
    chosen, gaps = [], []
    for index in range(len(pieces)):
        player, support = votes[index].most_common(1)[0] if votes[index] else (-1, 0)
        gap = min(margins[index][player], default=0.0) if player >= 0 else 0.0
        if support < CONSENSUS * sum(votes[index].values()) or pieces[index].conflicted:
            player = -1
        chosen.append(player)
        gaps.append(gap)
    # A label is attached to a whole tracklet, so local assignments alone are
    # insufficient: remove every weaker collision across the complete occupancy.
    for window in occupancy:
        by_player: dict[int, list[int]] = defaultdict(list)
        for index in window:
            if chosen[index] >= 0:
                by_player[chosen[index]].append(index)
        for members in by_player.values():
            members.sort(
                key=lambda i: (i in confirmed, gaps[i], pieces[i].weight), reverse=True
            )
            for index in members[1:]:
                chosen[index] = -1
    for index, player in confirmed.items():
        chosen[index], gaps[index] = player, ANCHOR_SCORE
    return chosen, gaps


@dataclass(frozen=True)
class Review:
    """One solve's recipe settings plus reviewer exclusions and cancellation.

    ``excluded`` views were marked by a reviewer as not a roster player; they
    keep their live occupancy but can never take a roster class.
    """

    settings: Settings
    stopped: Callable[[], bool] | None = None
    excluded: frozenset[str] = frozenset()


def identify(
    pieces: list[Fragment],
    roster: list[Player],
    anchors: list[Anchor],
    *,
    settings: Settings,
    stopped: Callable[[], bool] | None = None,
) -> dict:
    """Solve against named classes and expose calibrated or abstained assignments."""
    return solve(pieces, roster, anchors, Review(settings, stopped))


def barred_rows(
    indices: dict[str, int], checked: dict[str, Anchor], excluded: frozenset[str]
) -> list[int]:
    """Locate reviewer-excluded views, which can never also be confirmed.

    Returns:
        Row indices whose roster scores are forbidden.

    Raises:
        ValueError: A view is both confirmed and excluded.

    """
    if excluded & set(checked):
        raise ValueError("A confirmed view cannot also be excluded")
    return [indices[identity] for identity in excluded & set(indices)]


def mark_excluded(result: dict, excluded: frozenset[str]) -> None:
    """Publish reviewer-excluded views without any roster candidate."""
    for row in result["assignments"]:
        if row["identity"] in excluded:
            row.update(
                player_id=None,
                display_id=None,
                status="excluded",
                source="reviewer_excluded",
                candidate_player_id=None,
                margin=None,
                candidates=[],
            )


def solve(
    pieces: list[Fragment], roster: list[Player], anchors: list[Anchor], review: Review
) -> dict:
    """Solve one review state; see ``identify`` and ``Review``.

    Raises:
        ValueError: Evidence is unbounded, malformed or owned by a different recipe.

    """
    np = importlib.import_module("numpy")
    start = time.monotonic()
    settings = review.settings
    external_stop = review.stopped or (lambda: False)

    def expired() -> bool:
        return external_stop() or time.monotonic() - start >= settings.deadline

    if len(pieces) > settings.max_tracklets or len({p.identity for p in pieces}) != len(
        pieces
    ):
        raise ValueError("Closed-set review needs unique bounded tracklets")
    calibration = settings.calibration
    descriptor_recipe = settings.descriptor_recipe
    if calibration and calibration.descriptor_recipe != descriptor_recipe:
        raise ValueError("Calibration belongs to a different appearance recipe")
    checked = checked_anchors(pieces, roster, anchors)
    players = sorted({a.player_id for a in checked.values()})
    # Roster players lacking a confirmation have no classifier class. They must
    # stay unknown rather than receive a guessed label from another starter.
    indices = {p.identity: i for i, p in enumerate(pieces)}
    confirmed = {
        indices[a.identity]: players.index(a.player_id) for a in checked.values()
    }
    barred = barred_rows(indices, checked, review.excluded)
    values, means = projected_values(pieces)
    occupancy = windows(pieces)
    known = dict(confirmed)
    chosen, gaps = [-1] * len(pieces), [0.0] * len(pieces)
    scores = np.zeros((len(pieces), len(players)))
    if players:
        for round_index in range(ROUNDS + 1):
            if expired():
                return {"version": 1, "status": "stopped", "assignments": []}
            scores = fit_scores(values, means, known, len(players))
            add_scores(scores, pieces, players, roster, confirmed)
            scores[barred] = FORBIDDEN
            solution = windowed_assignment(
                scores, Windows(occupancy, settings.workers), confirmed, pieces, expired
            )
            if solution is None:
                return {"version": 1, "status": "stopped", "assignments": []}
            chosen, gaps = solution
            # Retain only the current solve's safe labels; never accumulate stale
            # pseudo labels from an earlier round or use an unconfirmed class.
            known = dict(confirmed)
            for index, player in enumerate(chosen):
                if (
                    player >= 0
                    and gaps[index] >= SELF_TRAIN_MARGIN
                    and len(values[index]) >= MIN_SAMPLES
                    and (
                        round_index > 0
                        or any(
                            p == player
                            and nearest_gap(pieces[index].times, pieces[i].times)
                            <= NEIGHBOUR_SECONDS
                            for i, p in confirmed.items()
                        )
                    )
                ):
                    known[index] = player
    result = publish(pieces, players, confirmed, (scores, chosen, gaps), settings)
    preserve_confirmations(result, checked, roster)
    mark_excluded(result, review.excluded)
    return {**result, "elapsed_seconds": time.monotonic() - start}


def preserve_confirmations(
    result: dict, checked: dict[str, Anchor], roster: list[Player]
) -> None:
    """Keep each verified source and its actual confidence in the output receipt."""
    labels = {
        p.player_id: f"#{p.number}" if p.number is not None else p.player_id
        for p in roster
    }
    for row in result["assignments"]:
        if row["player_id"]:
            row["display_id"] = labels[row["player_id"]]
        anchor = checked.get(row["identity"])
        if anchor:
            row["source"], row["confidence"] = anchor.source, anchor.confidence
            # Shirt numbers, and gallery views that came from one, name
            # players without a human: automatic.
            row["origin"] = (
                "automatic" if anchor.source in AUTOMATIC_SOURCES else "human"
            )


def projected_values(pieces: list[Fragment]) -> tuple[list[NDArray[Any]], NDArray[Any]]:
    """Validate and normalize cached per-crop match-space descriptors.

    Raises:
        ValueError: Descriptor arrays are malformed or outside the space bound.

    """
    np = importlib.import_module("numpy")
    values = [samples(p) for p in pieces]
    dimensions = {x.shape[1] for x in values if x.size and x.ndim == SAMPLE_AXES}
    if len(dimensions) > 1 or any(
        x.size
        and (x.ndim != SAMPLE_AXES or len(x) > MAX_SAMPLES or not np.isfinite(x).all())
        for x in values
    ):
        raise ValueError(
            "Descriptors must be finite, bounded samples in one fitted space"
        )
    dimension = next(iter(dimensions), 0)
    if dimension > MAX_DIMENSIONS:
        raise ValueError("Project descriptors into the bounded match space first")
    means = np.asarray([
        unit(x.mean(axis=0)) if x.size else np.zeros(dimension) for x in values
    ])
    return values, means


def publish(
    pieces: list[Fragment],
    players: list[str],
    confirmed: dict[int, int],
    solution: tuple[NDArray[Any], list[int], list[float]],
    settings: Settings,
) -> dict:
    """Publish confirmed names or empirical confidence; keep raw scores separate."""
    np = importlib.import_module("numpy")
    scores, chosen, gaps = solution
    calibration, descriptor_recipe = settings.calibration, settings.descriptor_recipe
    assignments = []
    for index, piece in enumerate(pieces):
        player = players[chosen[index]] if chosen[index] >= 0 else None
        anchored = index in confirmed
        # The calibration applies to the footage it was measured on only.
        measured = calibration is not None and not piece.identity.startswith(
            settings.uncalibrated
        )
        confidence = (
            1.0
            if anchored
            else calibration.probability(gaps[index])
            if calibration and measured and player
            else None
        )
        accepted = anchored or bool(
            confidence is not None and confidence >= settings.minimum_confidence
        )
        assignments.append({
            "identity": piece.identity,
            "player_id": player if accepted else None,
            "display_id": f"match:{player}" if accepted else None,
            "status": "anchored" if anchored else "assigned" if accepted else "unknown",
            # Who named this tracklet: a person ("human"), the engine on its
            # own ("automatic": shirt number or calibrated appearance), or
            # nobody yet. Clients must show automatic names as such.
            "origin": "human" if anchored else "automatic" if accepted else None,
            "confidence": confidence,
            "source": "verified_anchor"
            if anchored
            else "closed_set"
            if accepted
            else "unresolved",
            "candidate_player_id": player,
            "margin": float(gaps[index]) if not anchored else None,
            "calibrated": measured or anchored,
            "candidates": [
                {"player_id": players[int(j)], "score": float(scores[index, j])}
                for j in np.argsort(scores[index])[-3:][::-1]
            ],
        })
    return {
        "version": 1,
        "status": "completed",
        "solver": "hungarian_windows_whole_tracklet_exclusivity",
        "classifier_recipe": RECIPE,
        "descriptor_recipe": descriptor_recipe,
        "confidence_kind": "empirical_margin_precision"
        if calibration
        else "uncalibrated",
        "calibration": calibration.provenance if calibration else None,
        "review_only": True,
        "assignments": assignments,
        "fragments": len(pieces),
        "confirmed_classes": len(players),
    }


class ReviewSession:
    """Cached evidence for quick incremental answers; no detector or embedding rerun.

    Besides confirmations ("this view is player P") a reviewer can dismiss a
    view (not a player, the referee, or cannot tell), undo an answer, and add a
    free-form roster player.

    The roster uses roster sides; views carry anonymous kit tags. Which side
    wears which kit is re-decided on every solve from the recording's evidence
    (``clip_number_anchors.orientation``): this section's confirmations and
    one-side numbers, plus the ledger of the replay's other sections. Shirt-
    number reads stay kit evidence and become anchors only under a decided
    orientation; every solve drops those a confirmation or dismissal overrules.
    While the orientation is undecided, kit-tagged views get no name from
    numbers or appearance, and carried gallery views another section named by
    number are not loaded; confirmations always name their own view.
    """

    def __init__(
        self,
        scope: Scope,
        pieces: list[Fragment],
        roster: list[Player],
        settings: Settings,
        evidence: Evidence | list[Anchor] | None = None,
    ) -> None:
        """Fence this review to server-owned recording/run evidence and recipe.

        A plain list of anchors is evidence without shirt-number reads.

        Raises:
            ValueError: Number evidence was passed as anchors, or the
                evidence's dismissals are malformed.

        """
        self.namespace, self.fingerprint = scope.namespace, scope.fingerprint
        self.lock = RLock()
        self.pieces, self.roster = pieces, roster
        self.settings = settings
        if not isinstance(evidence, Evidence):
            evidence = Evidence(tuple(evidence or ()))
        if any(a.source == NUMBER_SOURCE for a in evidence.anchors):
            raise ValueError("Shirt numbers are kit evidence, not roster anchors")
        given = evidence.anchors
        self.anchors = [a for a in given if a.source not in CARRIED_SOURCES]
        self.carried = [a for a in given if a.source in CARRIED_SOURCES]
        self.reads = list(evidence.numbers)
        self.kits = evidence.kits
        self.section = evidence.section
        self.ledger = [s for s in evidence.ledger if s.section != self.section]
        dismissals = dict(evidence.dismissals)
        if not valid_dismissals(dismissals):
            raise ValueError("Invalid dismissals")
        self.dismissals: dict[str, str] = dismissals
        self.revision = 0
        self.receipts: dict[str, tuple[str, int]] = {}
        self.result = self.solve(self.anchors, self.dismissals, self.roster)

    @property
    def swapped(self) -> bool:
        """Whether roster team_a currently wears the engine's team_b kit."""
        return bool(self.result.get("orientation", {}).get("swapped"))

    @property
    def oriented(self) -> bool:
        """Whether the kit orientation is decided by evidence or options."""
        return self.result.get("orientation", {}).get("swapped") is not None

    def sightings(
        self,
        anchors: list[Anchor] | None = None,
        dismissals: dict[str, str] | None = None,
        roster: list[Player] | None = None,
    ) -> list[Sighting]:
        """Return this section's kit-orientation sightings for one review state.

        The anchor layer's corrections apply: a dismissed view gives none, and
        a read that would put a confirmed player on a second simultaneous view
        is overruled like the number anchor it would make.
        """
        anchors = self.anchors if anchors is None else anchors
        dismissals = self.dismissals if dismissals is None else dismissals
        pieces = {p.identity: p for p in self.pieces}
        found = sightings(
            [r for r in self.reads if r.identity not in dismissals],
            self.roster if roster is None else roster,
            [
                (a.identity, pieces[a.identity].team, a.player_id)
                for a in anchors
                if a.identity in pieces and a.identity not in dismissals
            ],
            section=self.section,
        )
        return [
            s
            for s in found
            if s.kind != NUMBER
            or not any(
                a.player_id == s.player_id
                and a.identity in pieces
                and overlap(pieces[a.identity], pieces[s.view])
                for a in anchors
            )
        ]

    def orient(
        self, anchors: list[Anchor], dismissals: dict[str, str], roster: list[Player]
    ) -> Orientation:
        """Decide the kit orientation for one candidate review state."""
        if self.kits:
            return Orientation(False, "options", {}, state="options")
        return orientation(
            [*self.sightings(anchors, dismissals, roster), *self.ledger], roster
        )

    def present(self, kits: Orientation) -> list[Anchor]:
        """Carried gallery views this orientation lets name a player.

        A confirmed view always names its player; a view another section named
        by number only under the orientation it was named under (or under any,
        when it had no kit tag).
        """
        state = None if not kits.decided else "swapped" if kits.swapped else "kept"
        return [
            a
            for a in self.carried
            if a.source == GALLERY_SOURCE or a.named_under in {"any", state} - {None}
        ]

    def solve(
        self,
        anchors: list[Anchor],
        dismissals: dict[str, str],
        roster: list[Player],
        *,
        stopped: Callable[[], bool] | None = None,
    ) -> dict:
        """Re-solve the cached space for one candidate review state.

        A swapped orientation means roster team_a wears the engine's team_b kit;
        the solver compares kits, so both roster and anchors are viewed by kit.
        """
        kits = self.orient(anchors, dismissals, roster)
        own, sides = list(anchors), list(roster)
        carried = self.present(kits)
        numbers, checks = self.checked_numbers(
            own, carried, self.standing(own, dismissals, roster, kits), roster
        )
        anchors = [
            *own,
            *[a for a in carried if a.player_id not in checks["absent_players"]],
            *numbers,
        ]
        if kits.swapped:
            roster = [replace(p, team=OTHER_KIT[p.team]) for p in roster]
            anchors = [replace(a, team=OTHER_KIT[a.team]) for a in anchors]
        result = solve(
            self.pieces,
            roster,
            anchors,
            Review(
                self.settings,
                stopped,
                frozenset(i for i, r in dismissals.items() if r in EXCLUDING),
            ),
        )
        if result.get("status") != "completed":
            return result
        if not kits.decided:
            withhold_kit_names(result, self.pieces)
        withhold(result, set(checks["contradicted"]), "number_contradicted")
        suspects = flag_kit_suspects(result, self.pieces, own, kits, sides)
        return {
            **result,
            "orientation": {**kits.receipt(), "suspect_views": suspects},
            "number_checks": checks,
        }

    def checked_numbers(
        self,
        own: list[Anchor],
        carried: list[Anchor],
        numbers: list[Anchor],
        roster: list[Player],
    ) -> tuple[list[Anchor], dict[str, list[str]]]:
        """Keep the number anchors appearance does not contradict, of present players.

        First every ambiguous number anchor is checked against the appearance
        of the rest of the recording (``contradicted``); then players whose remaining
        anchors misreads alone explain are absent
        (``clip_number_anchors.absent``). Confirmed players, here or in a
        carried gallery view, are always present.
        """
        trusted = [*own, *carried]
        wrong = contradicted(self.pieces, numbers, trusted, self.settings, roster)
        kept = [a for a in numbers if a.identity not in wrong]
        attested = {a.player_id for a in trusted if a.source not in AUTOMATIC_SOURCES}
        automatic = [a for a in carried if a.source in AUTOMATIC_SOURCES]
        gone = absent([*kept, *automatic], roster, attested)
        return [a for a in kept if a.player_id not in gone], {
            "contradicted": sorted(wrong),
            "absent_players": sorted(gone),
        }

    def standing(
        self,
        anchors: list[Anchor],
        dismissals: dict[str, str],
        roster: list[Player],
        kits: Orientation,
    ) -> list[Anchor]:
        """Return the number anchors no confirmation or dismissal overrules."""
        automatic, _ = named(self.pieces, self.reads, roster, kits.swapped)
        kept, _ = respecting(automatic, anchors, self.pieces)
        return [a for a in kept if a.identity not in dismissals]

    def recarry(
        self,
        anchors: list[Anchor],
        ledger: list[Sighting] | None = None,
        added: list[Player] | None = None,
    ) -> None:
        """Replace the carried gallery anchors and other sections' sightings.

        All of these change when another section's reviewer answers, as do the
        recording's players: ``added`` are players another section's reviewer
        added to the roster. Re-solve.

        Raises:
            ValueError: An anchor is not carried evidence or the review cannot solve.

        """
        if any(a.source not in CARRIED_SOURCES for a in anchors):
            raise ValueError("Only carried gallery players can be replaced")
        previous = self.carried, self.ledger, self.roster
        with self.lock:
            self.carried = list(anchors)
            if ledger is not None:
                self.ledger = [s for s in ledger if s.section != self.section]
            self.roster = [*self.roster, *(added or [])]
            result = self.solve(self.anchors, self.dismissals, self.roster)
            if result["status"] != "completed":
                self.carried, self.ledger, self.roster = previous
                raise ValueError("Revised carried players could not be solved")
            self.result = result

    def questions(self, limit: int = 12) -> list[dict]:
        """Rank long ambiguous or novel views for confirmation without hidden truth."""
        assignments = self.result.get("assignments", [])
        # Confirmed and number-anchored views need no question.
        confirmed = {a["identity"] for a in assignments if a["status"] == "anchored"}
        pieces = {p.identity: p for p in self.pieces}
        result: list[dict[str, Any]] = []
        for row in assignments:
            piece = pieces[row["identity"]]
            if not self.askable(piece, confirmed):
                continue
            # Weight expected correction impact and uncertainty. Unrepresented
            # classes cannot be guessed, so long unknown views rank first.
            # Raw margins rank questions without being presented as probabilities.
            # This also changes the queue after each confirmation, even while the
            # public acceptance policy is intentionally uncalibrated.
            uncertainty = (
                1 / (1 + 20 * max(0.0, row["margin"] or 0))
                if row["player_id"] is None
                else 1 - (row["confidence"] or 0)
            )
            priority = piece.weight * max(0.01, uncertainty)
            moments = sorted(piece.times)
            result.append({
                "identity": piece.identity,
                "time_seconds": moments[len(moments) // 2],
                "source_track_id": piece.representative_track_id,
                "start": moments[0],
                "end": moments[-1],
                "team": piece.team,
                "candidate_player_id": row["candidate_player_id"],
                "candidates": row["candidates"],
                "priority": priority,
                "reason": "uncertain_assignment"
                if row["candidate_player_id"]
                else "unrepresented_view",
            })
        return sorted(result, key=lambda q: (-q["priority"], q["identity"]))[
            : max(0, min(limit, 32))
        ]

    def askable(self, piece: Fragment, confirmed: set[str]) -> bool:
        """Whether a view may become a question: unanswered, live and in play.

        Only people in live play are asked about: a team kit, not placed off
        the court (``clip_live_play``).
        """
        return not (
            piece.identity in confirmed
            or piece.identity in self.dismissals
            or piece.replay
            or piece.conflicted
            or not piece.times
        ) and in_play(piece.team, piece.placement)

    def naming(self) -> dict[str, dict]:
        """Classify every view as confirmed, automatic, excluded or unknown.

        Automatic names are calibrated acceptances or raw candidates at the
        frozen research proposal margin; review tools must mark them as such.
        """
        labels = {
            p.player_id: f"#{p.number}" if p.number is not None else p.player_id
            for p in self.roster
        }
        result = {}
        for row in self.result.get("assignments", []):
            reason = self.dismissals.get(row["identity"])
            player = None
            if row["status"] == "anchored" and row.get("origin") == "automatic":
                state, player = "automatic", row["player_id"]
            elif row["status"] == "anchored":
                state, player = "confirmed", row["player_id"]
            elif reason in EXCLUDING:
                state = reason
            elif row["player_id"]:
                state, player = "automatic", row["player_id"]
            elif (
                # Raw research proposals only where no calibration exists.
                not row.get("withheld")
                and not row.get("calibrated")
                and row.get("candidate_player_id")
                and (row.get("margin") or 0) >= PROPOSAL_MARGIN
            ):
                state, player = "automatic", row["candidate_player_id"]
            else:
                state = "unknown"
            result[row["identity"]] = {
                "naming": state,
                "player_id": player,
                "display_id": labels.get(player, player) if player else None,
                "margin": row.get("margin"),
                "unclear": reason == "unclear",
            }
        return result

    def summary(self, naming: dict[str, dict] | None = None) -> dict:
        """Count live player observations by naming state, plus roster progress."""
        naming = naming or self.naming()
        counts: Counter[str] = Counter()
        for piece in self.pieces:
            if piece.replay or not piece.times or piece.identity not in naming:
                continue
            counts[naming[piece.identity]["naming"]] += len(piece.times)
        per_player = Counter(a.player_id for a in self.anchors)
        return {
            "observations": counts["confirmed"]
            + counts["automatic"]
            + counts["unknown"],
            "confirmed": counts["confirmed"],
            "automatic": counts["automatic"],
            "unknown": counts["unknown"],
            "excluded": counts["referee"] + counts["not_player"],
            "confirmations": len(self.anchors),
            "dismissals": len(self.dismissals),
            "players_confirmed": len(per_player),
            "per_player": dict(per_player),
        }

    def snapshot(self) -> dict:
        """Return a versioned question queue and immutable identity proposal overlay."""
        return deepcopy({
            "version": 1,
            "namespace": self.namespace,
            "fingerprint": self.fingerprint,
            "revision": self.revision,
            "result": self.result,
            "anchors": [a.__dict__ for a in self.anchors],
            "automatic_anchors": sum(
                row["status"] == "anchored" and row.get("source") == NUMBER_SOURCE
                for row in self.result.get("assignments", [])
            ),
            "dismissals": self.dismissals,
            "questions": self.questions(),
        })

    def state(self) -> dict:
        """Serialize bounded confirmations for owner-leased caller persistence."""
        return {
            "version": STATE_VERSION,
            "namespace": self.namespace,
            "fingerprint": self.fingerprint,
            "descriptor_recipe": self.settings.descriptor_recipe,
            "revision": self.revision,
            "anchors": [a.__dict__ for a in self.anchors],
            "carried": [a.__dict__ for a in self.carried],
            "numbers": [asdict(r) for r in self.reads],
            "kits": self.kits,
            "section": self.section,
            "ledger": [asdict(s) for s in self.ledger],
            "dismissals": self.dismissals,
            "receipts": self.receipts,
        }

    def restore(self, state: dict) -> None:
        """Restore confirmations and evidence only onto identical server evidence.

        Raises:
            ValueError: The persisted state is malformed or belongs to another review.

        """
        expected = self.namespace, self.fingerprint, self.settings.descriptor_recipe
        if (
            state.get("version") != STATE_VERSION
            or (
                state.get("namespace"),
                state.get("fingerprint"),
                state.get("descriptor_recipe"),
            )
            != expected
        ):
            raise ValueError("Persisted review belongs to different evidence")
        revision = state.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("Invalid persisted review revision")
        anchors = state.get("anchors")
        carried = state.get("carried", [])
        numbers = state.get("numbers", [])
        receipts = state.get("receipts", {})
        dismissals = state.get("dismissals", {})
        kits = state.get("kits", False)
        section = state.get("section", "")
        ledger = state.get("ledger", [])
        if (
            not valid_dismissals(dismissals)
            or not isinstance(kits, bool)
            or not isinstance(section, str)
        ):
            raise ValueError("Invalid persisted review answers")
        if not (
            bounded(anchors, MAX_RECEIPTS)
            and bounded(carried, MAX_TRACKLETS)
            and bounded(numbers, MAX_TRACKLETS)
            and bounded(ledger, MAX_LEDGER)
            and isinstance(receipts, dict)
            and len(receipts) <= MAX_RECEIPTS
        ):
            raise ValueError("Persisted review exceeds its bound")
        pending = [Anchor(**a) for a in anchors or []]
        remembered = [Anchor(**a) for a in carried]
        reads = [KitRead(**r) for r in numbers]
        others = [Sighting(**s) for s in ledger]
        if any(a.source in CARRIED_SOURCES | {NUMBER_SOURCE} for a in pending) or any(
            a.source not in CARRIED_SOURCES for a in remembered
        ):
            raise ValueError("Persisted answers and carried players are mixed up")
        if any(r.kit not in {*TEAMS, None} for r in reads):
            raise ValueError("Persisted shirt-number reads need a kit tag or none")
        with self.lock:
            self.carried, self.reads, self.kits = remembered, reads, kits
            self.section = section
            self.ledger = [s for s in others if s.section != section]
        result = self.solve(pending, dismissals, self.roster)
        if result["status"] != "completed":
            raise ValueError("Persisted review could not be solved")
        with self.lock:
            self.result, self.anchors, self.revision = result, pending, revision
            self.dismissals = dict(dismissals)
            self.receipts = {
                key: (str(value[0]), int(value[1])) for key, value in receipts.items()
            }

    def validated(self, payload: dict) -> str:
        """Check the versioned envelope and return the requested action.

        Raises:
            ValueError: The envelope is malformed, unbounded or cross-owned.

        """
        action = payload.get("action", "confirm")
        envelope = {
            "version",
            "namespace",
            "fingerprint",
            "expected_revision",
            "request_id",
        } | ({"action"} if "action" in payload else set())
        if (
            not isinstance(action, str)
            or action not in ANSWER_FIELDS
            or set(payload) != envelope | ANSWER_FIELDS[action]
            or payload["version"] != 1
        ):
            raise ValueError("Invalid versioned confirmation")
        if (payload["namespace"], payload["fingerprint"]) != (
            self.namespace,
            self.fingerprint,
        ):
            raise ValueError("Confirmation belongs to different review evidence")
        request_id = payload["request_id"]
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= MAX_REQUEST_ID
        ):
            raise ValueError("Confirmation needs a bounded request ID")
        return action

    def proposed(
        self, action: str, payload: dict
    ) -> tuple[list[Anchor], dict[str, str], list[Player]]:
        """Build the review state one answer would produce, without committing it.

        The kit orientation is not part of it: every solve re-decides it.

        Returns:
            Anchors, dismissals and roster.

        Raises:
            ValueError: The answer names an unknown view or player.

        """
        anchors, dismissals = list(self.anchors), dict(self.dismissals)
        roster = list(self.roster)
        pieces = {p.identity: p for p in self.pieces}
        identity = payload.get("identity")
        if action != "add_player" and (
            not isinstance(identity, str) or identity not in pieces
        ):
            raise ValueError("Unknown review view")
        if action == "add_player":
            roster.append(new_player(payload["player"], roster))
        elif action == "confirm":
            player = next(
                (p for p in roster if p.player_id == payload["player_id"]), None
            )
            if player is None:
                raise ValueError("Choose a player in this match roster")
            anchors = [a for a in anchors if a.identity != identity] + [
                Anchor(str(identity), player.player_id, player.team, player.number)
            ]
            dismissals.pop(str(identity), None)
        elif action == "dismiss":
            if payload["reason"] not in DISMISSALS:
                raise ValueError("Choose not a player, referee or cannot tell")
            anchors = [a for a in anchors if a.identity != identity]
            dismissals[str(identity)] = payload["reason"]
        else:
            if identity not in dismissals and all(
                a.identity != identity for a in anchors
            ):
                raise ValueError("Nothing to undo for this view")
            anchors = [a for a in anchors if a.identity != identity]
            dismissals.pop(str(identity), None)
        return anchors, dismissals, roster

    def answer(
        self, payload: dict, *, stopped: Callable[[], bool] | None = None
    ) -> dict:
        """Apply one idempotent answer atomically after a successful re-solve.

        Raises:
            ValueError: A request is stale, cross-owned or reuses different content.

        """
        action = self.validated(payload)
        request_id = payload["request_id"]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        if request_id in self.receipts:
            prior, revision = self.receipts[request_id]
            if prior != digest:
                raise ValueError("Request ID reused for another confirmation")
            return {**self.snapshot(), "applied_revision": revision}
        if (
            isinstance(payload["expected_revision"], bool)
            or payload["expected_revision"] != self.revision
        ):
            raise ValueError("Review revision conflict; refresh before confirming")
        anchors, dismissals, roster = self.proposed(action, payload)
        result = self.solve(anchors, dismissals, roster, stopped=stopped)
        if result["status"] != "completed" or (stopped and stopped()):
            return {"version": 1, "status": "stopped", "revision": self.revision}
        self.anchors, self.dismissals, self.result = anchors, dismissals, result
        self.roster = roster
        self.revision += 1
        snapshot = self.snapshot()
        self.receipts[request_id] = digest, self.revision
        # Bound duplicate receipts while retaining the most recent user retries.
        if len(self.receipts) > MAX_RECEIPTS:
            del self.receipts[next(iter(self.receipts))]
        return {**snapshot, "applied_revision": self.revision}


def flag_kit_suspects(
    result: dict,
    pieces: list[Fragment],
    confirmed: list[Anchor],
    kits: Orientation,
    roster: list[Player],
) -> list[str]:
    """Flag confirmed views whose kit tag contradicts a decided orientation.

    The reviewer named the person; under the recording's orientation that
    person's side wears the other kit, so the view's kit tag is the suspect
    (an outlier, never a reason to re-orient everybody).

    Returns:
        The flagged views.

    """
    if not kits.decided:
        return []
    kit_of = {p.identity: p.team for p in pieces}
    sides = {p.player_id: p.team for p in roster}
    suspects = sorted(
        a.identity
        for a in confirmed
        if kit_of.get(a.identity) in TEAMS
        and sides.get(a.player_id) in TEAMS
        and roster_side(kit_of[a.identity], bool(kits.swapped)) != sides[a.player_id]
    )
    flagged = set(suspects)
    for row in result["assignments"]:
        if row["identity"] in flagged:
            row["kit_suspect"] = True
    return suspects


def alternatives(anchor: Anchor, roster: list[Player]) -> set[str]:
    """Return the other roster players the same read could have come from.

    Two readings are plausible for some reads: a number both teams wear read
    under a wrong kit tag (the other side's wearer), and a partial read of a
    two-digit shirt (8 read from 18: a same-side player whose number contains
    the read).
    """
    if anchor.number is None:
        return set()
    return {
        p.player_id
        for p in roster
        if p.player_id != anchor.player_id
        and p.number is not None
        and (
            p.number == anchor.number
            or (
                p.team == anchor.team
                and len(p.number) > len(anchor.number)
                and anchor.number in p.number
            )
        )
    }


def contradicted(
    pieces: list[Fragment],
    numbers: list[Anchor],
    trusted: list[Anchor],
    settings: Settings,
    roster: list[Player],
) -> set[str]:
    """Return ambiguous number-anchored views whose appearance names the alternative.

    For an ambiguous read (``alternatives``: a shared number under a possibly
    wrong kit tag, a possibly partial read) the number cannot choose between
    two roster players; appearance can. Each anchor is held out in turn (folds
    of ``FOLDS``) and the classifier is fitted on the confirmations and all
    other anchors, across both teams. When an alternative leads the read's
    player (and nobody) by the descriptor's calibrated margin, the view names
    nobody. Appearance judges only between classes fitted on at least
    ``MIN_JUDGE_VIEWS`` other views each: raw classifier scores of a player
    seen once or twice are noise, not the calibrated solver margin. On the
    development clips (60 s, two or three anchors per player) a check of every
    read dropped correct reads (two of F21's on Fortuna 3300) and a check
    without that minimum dropped one (F21's #21 against D27's). Unambiguous
    reads are not judged. Without a calibrated descriptor nothing is judged.
    """
    calibration = settings.calibration
    margin = next(
        (
            boundary
            for boundary, precision in (calibration.bins if calibration else ())
            if precision >= settings.minimum_confidence
        ),
        None,
    )
    rivals = {
        # Appearance judges no read on footage its calibration does not cover.
        a.identity: set()
        if a.identity.startswith(settings.uncalibrated)
        else alternatives(a, roster)
        for a in numbers
    }
    if margin is None or not any(rivals.values()):
        return set()
    values, means = projected_values(pieces)
    index = {p.identity: i for i, p in enumerate(pieces)}
    players = sorted({a.player_id for a in [*trusted, *numbers]})
    column = {player: j for j, player in enumerate(players)}
    base = {
        index[a.identity]: column[a.player_id] for a in trusted if a.identity in index
    }
    order = sorted(
        (a for a in numbers if a.identity in index), key=lambda a: a.identity
    )
    folds = min(FOLDS, len(order))
    found: set[str] = set()
    for fold in range(folds):
        held = order[fold::folds]
        known = dict(base)
        for position, anchor in enumerate(order):
            if position % folds != fold:
                known[index[anchor.identity]] = column[anchor.player_id]
        scores = fit_scores(values, means, known, len(players))
        views = Counter(player for row, player in known.items() if len(values[row]))
        for anchor in held:
            options = [
                column[p]
                for p in rivals[anchor.identity]
                if p in column and views[column[p]] >= MIN_JUDGE_VIEWS
            ]
            if not options or views[column[anchor.player_id]] < MIN_JUDGE_VIEWS:
                continue
            row = scores[index[anchor.identity]]
            read = max(0.0, float(row[column[anchor.player_id]]))
            rival = max(float(row[j]) for j in options)
            # The solver's margin is half a row's score lead (``window_votes``).
            if (rival - read) / 2 >= margin:
                found.add(anchor.identity)
    return found


def withhold(result: dict, views: set[str], reason: str) -> None:
    """Keep appearance names of these views unpublished, saying why."""
    for row in result["assignments"]:
        if row["identity"] not in views or row["status"] == "anchored":
            continue
        row["withheld"] = reason
        if row["status"] == "assigned":
            row.update(
                player_id=None,
                display_id=None,
                status="unknown",
                origin=None,
                confidence=None,
                source="unresolved",
            )


def withhold_kit_names(result: dict, pieces: list[Fragment]) -> None:
    """Keep appearance names of kit-tagged views unpublished without orientation.

    The classifier compares kits; with no evidence of which roster side wears
    which kit, a name for a kit-tagged view could belong to the other team.
    """
    kits = {p.identity: p.team for p in pieces}
    for row in result["assignments"]:
        if row["status"] == "anchored" or kits.get(row["identity"]) not in TEAMS:
            continue
        row["withheld"] = "kit_orientation_unknown"
        if row["status"] == "assigned":
            row.update(
                player_id=None,
                display_id=None,
                status="unknown",
                origin=None,
                confidence=None,
                source="unresolved",
            )


def bounded(values: object, limit: int) -> bool:
    """Check a persisted list and its length bound."""
    return isinstance(values, list) and len(values) <= limit


def valid_dismissals(value: object) -> bool:
    """Check a persisted, bounded map of view identities to dismissal reasons.

    Returns:
        Whether every reason is a known dismissal.

    """
    return (
        isinstance(value, dict)
        and len(value) <= MAX_TRACKLETS
        and all(reason in DISMISSALS for reason in value.values())
    )


def new_player(row: object, roster: list[Player]) -> Player:
    """Validate one free-form roster addition (kit team, canonical number).

    Returns:
        The new roster player.

    Raises:
        ValueError: The player is malformed, duplicated or exceeds the roster.

    """
    if (
        not isinstance(row, dict)
        or set(row) - {"player_id", "team", "number"}
        or not isinstance(row.get("player_id"), str)
        or not 1 <= len(row["player_id"]) <= MAX_PLAYER_ID
        or row.get("team") not in TEAMS
    ):
        raise ValueError("A player needs an ID and team A or B")
    number = row.get("number")
    if number is not None and (
        not isinstance(number, str)
        or not number.isdigit()
        or str(int(number)) != number
        or int(number) > MAX_SHIRT
    ):
        raise ValueError("A shirt number has one or two digits")
    if len(roster) >= MAX_ROSTER or any(
        p.player_id == row["player_id"]
        or (number is not None and (p.team, p.number) == (row["team"], number))
        for p in roster
    ):
        raise ValueError("This player or shirt number is already on the roster")
    return Player(row["player_id"], row["team"], number)


def merge_additions(
    roster: list[Player], candidates: list[Player]
) -> tuple[list[Player], dict[str, str], list[Player]]:
    """Add players reviewers added while naming ("Speler toevoegen") to a roster.

    One policy for the whole recording (its sections, their reviews and the
    match pass): a player ID is one person; an addition wearing a shirt number
    its side already has is that player (two reviewers adding the same
    substitute, or a later linked line-up); an addition without a number is a
    person of its own. Additions beyond ``MAX_ROSTER`` are left out.

    Returns:
        The merged roster, aliases (addition ID -> roster ID) and the players
        that were added.

    """
    players = {p.player_id: p for p in roster}
    worn = {(p.team, p.number): p.player_id for p in roster if p.number is not None}
    aliases: dict[str, str] = {}
    added: list[Player] = []
    for player in candidates:
        if player.player_id in players or player.player_id in aliases:
            continue
        same = worn.get((player.team, player.number)) if player.number else None
        if same is not None:
            aliases[player.player_id] = same
            continue
        if len(players) >= MAX_ROSTER:
            continue
        players[player.player_id] = player
        added.append(player)
        if player.number is not None:
            worn[player.team, player.number] = player.player_id
    return list(players.values()), aliases, added


def automatic_anchors(
    pieces: list[Fragment], roster: list[Player], *, approved: bool = False
) -> list[Anchor]:
    """Promote only approved calibrated episode evidence above a strict threshold.

    Number mass retains unreadable/unknown probability; repeat snapshots do not
    add independent votes. A rejected artifact such as n1 cannot create anchors.
    """
    if not approved:
        return []
    by_number = {(p.team, p.number): p for p in roster if p.number is not None}
    result = []
    for piece in pieces:
        episodes = {n.episode: n for n in piece.numbers if n.calibrated}
        if not episodes:
            continue
        pooled: dict[str, float] = defaultdict(float)
        for reading in episodes.values():
            for number, probability in reading.probabilities.items():
                pooled[number] += probability * reading.readability / len(episodes)
        number, precision = max(pooled.items(), key=itemgetter(1), default=("", 0))
        player = by_number.get((piece.team, number))
        if player is not None and precision >= AUTOMATIC_THRESHOLD:
            candidate = Anchor(
                piece.identity,
                player.player_id,
                player.team,
                player.number,
                source="approved_shirt_number",
                confidence=precision,
            )
            try:
                checked_anchors(pieces, roster, [*result, candidate])
            except ValueError:
                continue
            result.append(candidate)
    return result


def session_from_gallery(
    gallery: object,
    scope: Scope,
    pieces: list[Fragment],
    roster: list[Player],
    settings: Settings,
) -> ReviewSession:
    """Load l2's owner-scoped confirmed galleries into a current-section review.

    Raises:
        ValueError: Gallery ownership/descriptor recipe differs from this review.

    """
    if (
        not isinstance(gallery, RecordingGallery)
        or gallery.namespace != scope.namespace
    ):
        raise ValueError("Gallery belongs to a different recording")
    with gallery.connect() as db:
        recipe = db.execute(
            "SELECT descriptor_recipe FROM recording WHERE id=1"
        ).fetchone()
        if recipe is None or recipe[0] != settings.descriptor_recipe:
            raise ValueError("Gallery belongs to a different descriptor recipe")
        carried, anchors = gallery.load(db)
    # l2's gallery holds confirmed players only, each in the kit it was
    # confirmed in: the recording's kit-orientation sightings.
    ledger = tuple(
        Sighting(a.player_id, piece.team, CONFIRMATION, a.identity, "gallery")
        for piece, a in zip(carried, anchors, strict=True)
        if piece.team in TEAMS
    )
    combined = deepcopy([*carried, *pieces])
    fit_fragments(combined, dimensions=MAX_DIMENSIONS)
    return ReviewSession(
        scope, combined, roster, settings, Evidence(tuple(anchors), ledger=ledger)
    )
