"""Shirt-number reads as automatic roster anchors, and the kit orientation they need.

The number reader emits one raw distribution per clear-torso read (another
person covering the torso blocks the read). Here the readable reads of one pure
tracklet are pooled with the artifact's agreement rule: the summed probability
of a number divided by ``max(reads, min_support)``, so one confident view can
never decide alone. A tracklet whose pooled number reaches the frozen threshold
names one ``(team, number)`` slot of the match roster: an automatic anchor.

Anchors that contradict each other are rejected together, never merged: two
live-simultaneous tracklets naming the same player, or a number whose team the
roster does not have. Human confirmations override numbers: a number anchor on
a confirmed tracklet, or one that collides with a confirmation of the same
player, is dropped.

Kit orientation: a small evidence model
---------------------------------------
Reads carry the recording's anonymous kit tags (``team_a``/``team_b`` shirt
colour groups); the roster has sides (home ``team_a``, away ``team_b``). Which
side wears which kit is one match-level hypothesis with two states, *kept*
(home wears kit ``team_a``) and *swapped*, plus *undecided*. It is never stored:
every solve re-decides it from all evidence of the recording (``orientation``),
and a kit-tagged read names a player only under a decided orientation.

Evidence is a set of sightings, "player P was seen in kit K" (``Sighting``):

- a reviewer's confirmation of a kit-tagged view: the person is certain, the
  view's kit tag may be wrong;
- a confident read of a number exactly one roster side wears, naming that
  side's wearer; a number both sides wear says nothing about the kits.

A view gives at most one sighting (a confirmation overrules its own read). The
anchor layer's corrections apply to sightings too: a dismissed view (not a
player, the referee, cannot tell) gives none, and neither does a read that
would put a confirmed player on a second simultaneous view. Sightings come from
this section and, through the recording gallery's ledger, from every other
section of a replay.

Independence. The views of one person share their systematic failures (a bib,
an odd undershirt, an opponent dominating the crop), so all sightings of one
player form one vote: the kit seen most often (a tie abstains). Votes of
different players are treated as independent; how often one is wrong is a
property of the recording (how distinct the two kits are) and is estimated.

Measured vote error. On the development clips (DSC-TOP, Fortuna, club) 1 of
43 automatic votes was wrong (2.3%; ``MEASURED_WRONG`` of ``MEASURED_VOTES``)
and none of 80 votes backed by confirmations, although one to five players per
clip had some crops in the wrong kit: a player's majority usually survives
them. 43 votes cannot exclude a per-vote error up to ``MAX_VOTE_ERROR``
(10.6%, one-sided 95% bound), and a recording with similar kits can be worse.

Decision. With ``k`` votes for the leading state and ``c`` against it, the
recording's vote error ``e`` is the development measurement (a Jeffreys prior
updated with it) updated with this recording's ``c`` dissenting of ``k + c``
votes, never below ``OBSERVATION_ERROR`` (``vote_error``). The leading state's
posterior is ``1 / (1 + (e / (1 - e)) ** (k - c))``, so dissent costs twice: it
narrows the margin and raises ``e``. The orientation is decided only when that
reaches ``1 - DECISION_RISK`` and the ``c`` contradictions are plausible at
some per-vote error the measurement allows (binomial tail at
``MAX_VOTE_ERROR`` at least ``CONTRADICTION_P``); otherwise it is *undecided*,
or *contradicted* when more players disagree than any such error explains.

Invariants (each tested in ``test_korfbal_clip_kit_orientation.py``):

1. One sighting never settles the orientation: a decision needs at least two
   independent players agreeing with it (the posterior needs ``k - c >= 2``).
2. One sighting never flips it: adding or removing a single sighting moves at
   most one player's vote by one step, so a decided state never becomes the
   opposite one.
3. A lone contradiction is an outlier: with at least three agreeing players and
   one against, the decision stands. The dissenting player is reported in
   ``outliers``, a confirmed view in the contradicting kit is flagged
   ``kit_suspect``, and the orientation and every number name stay as they
   were. (The confirmation still trains the appearance classifier, as every
   confirmation does, which can move borderline appearance names; a wrong
   kit tag adds nothing to that.)
4. Sustained contradiction re-opens the hypothesis (*contradicted*).
5. Order independence: the decision depends on the set of sightings only.
6. A confirmation always names its own view, whatever the orientation state.
7. Without a decision nothing kit-dependent is named: no number anchor of a
   kit-tagged view, no appearance name of one, and no automatic gallery view
   that was named under an orientation the recording no longer holds, in any
   section (``clip_section_identity`` keeps each view's basis).

Presence: absent players stay absent
------------------------------------
A roster can list players who never play (a squad rather than a line-up, an
unused substitute). Misreads still land on their slots now and then, and over a
whole match those few stray anchors made a class that the class-balanced
classifier then filled with everything nobody else explains (f1: two absent
players named 1,067 observations, 55 of 58 sampled crops wrong). So a player
whose number anchors are no more than misreads explain is *absent* (``absent``):
none of their number anchors names anyone, and they get no appearance class
until a reviewer confirms them.

With ``N`` number anchors over ``S`` numbered roster slots, misreads alone put
``lambda = NUMBER_READ_ERROR * N / (S - 1)`` anchors on a slot (Poisson). A
player who played an unknown share ``f`` of the time collects about
``mu * f`` anchors, ``mu = N / S`` being a full-time player's share; with ``f``
uniform the likelihood of ``k`` anchors is ``P(Poisson(mu) > k) / mu``. The
player is present when that is at least the misread likelihood of ``k``. In a
60-second broadcast clip (``N`` about 40) one anchor is enough; over an
80-minute match (``N`` about 2,500) a player needs about eight.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
import math
from typing import Any

from .clip_match_identity import Anchor, Fragment, overlap


TEAMS = ("team_a", "team_b")
SWAPPED = {"team_a": "team_b", "team_b": "team_a"}
SWAPPED_STATE = {"kept": "swapped", "swapped": "kept"}
MIN_LEGIBILITY = 0.5
MIN_PROBABILITY = 0.005
MAX_READS = 400_000
# Shirt-colour teams are 96-99% right on broadcast; take the worst case.
KIT_TAG_ERROR = 0.04
# Roster-restricted number anchors were ~98% right on the development clips.
NUMBER_READ_ERROR = 0.02
# One player's vote is wrong if its kit tag or (for a number) its read is; the
# least per-vote error a recording is assumed to have.
OBSERVATION_ERROR = KIT_TAG_ERROR + NUMBER_READ_ERROR
# Automatic votes on the five development clips (z2, 2026-10-04), against the
# labelled players' teams: one wrong (DSC-TOP 3300, a player whose few reads
# came from his one mis-tagged tracklet).
MEASURED_VOTES = 43
MEASURED_WRONG = 1
# A decided orientation leaves the other one at most this posterior: below the
# 2% error budget of a published automatic name (two agreeing players: 0.41%).
DECISION_RISK = 0.005
# More contradicting players than this tail probability allows re-open it.
CONTRADICTION_P = 0.05
BOUND_CONFIDENCE = 0.95
CONFIRMATION = "confirmation"
NUMBER = "number"
MIN_THRESHOLD = 0.5
MIN_SUPPORT = 2
SOURCE = "shirt_number"


@dataclass(frozen=True)
class NumberPolicy:
    """Frozen acceptance rule of one reader artifact for roster-restricted anchors."""

    threshold: float
    min_support: int
    provenance: str

    @classmethod
    def from_receipt(cls, receipt: Mapping[str, Any] | None) -> NumberPolicy | None:
        """Read an approved policy from the number artifact's run receipt."""
        policy = (receipt or {}).get("roster_anchors") or {}
        rule = usable(policy.get("threshold"), policy.get("min_support"))
        if policy.get("approved") is not True or rule is None:
            return None
        return cls(*rule, str((receipt or {}).get("manifest_sha256", "unknown")))


def usable(threshold: object, support: object) -> tuple[float, int] | None:
    """Require a majority threshold and at least two agreeing reads."""
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return None
    if isinstance(support, bool) or not isinstance(support, int):
        return None
    if not math.isfinite(threshold) or not MIN_THRESHOLD < threshold <= 1:
        return None
    return (float(threshold), support) if support >= MIN_SUPPORT else None


class NumberReads:
    """Readable crop distributions per ``(time, track)``, compact and bounded."""

    def __init__(self, limit: int = MAX_READS) -> None:
        """Keep at most ``limit`` readable reads; later ones are counted, not kept."""
        self.reads: dict[tuple[float, str], dict[str, float]] = {}
        self.limit = limit
        self.dropped = 0

    def observe(self, persons: Iterable[Mapping[str, Any]], time: float) -> None:
        """Record this frame's readable reads; hidden shirts do not vote."""
        for person in persons:
            values = person.get("shirt_number_observation")
            track = person.get("track_id")
            if not values or not track:
                continue
            if 1 - float(values.get("unknown", 0.0)) < MIN_LEGIBILITY:
                continue
            if len(self.reads) >= self.limit:
                self.dropped += 1
                continue
            self.reads[round(time, 6), str(track)] = {
                k: float(v)
                for k, v in values.items()
                if k != "unknown" and v >= MIN_PROBABILITY
            }

    def snapshot(self) -> dict[str, int]:
        """Receipt counts."""
        return {"readable_reads": len(self.reads), "dropped_reads": self.dropped}


def pooled(reads: Sequence[Mapping[str, float]], min_support: int) -> dict[str, float]:
    """Agreement pooling: one view contributes at most ``1 / min_support``."""
    if not reads:
        return {}
    count = max(len(reads), min_support)
    total: dict[str, float] = defaultdict(float)
    for read in reads:
        for number, probability in read.items():
            total[number] += probability / count
    return dict(total)


def strongest(values: Mapping[str, float]) -> tuple[str | None, float]:
    """Most probable number and its pooled mass."""
    if not values:
        return None, 0.0
    number = max(values, key=lambda k: (values[k], k))
    return number, values[number]


def tracklet_reads(
    owners: Mapping[tuple[float, str], str], reads: NumberReads
) -> dict[str, list[dict[str, float]]]:
    """Group reads under the pure tracklet that owns each ``(time, track)`` row."""
    grouped: dict[str, list[dict[str, float]]] = defaultdict(list)
    for key, values in reads.reads.items():
        identity = owners.get(key)
        if identity is not None:
            grouped[identity].append(values)
    return grouped


def confident(
    pieces: list[Fragment],
    grouped: Mapping[str, list[dict[str, float]]],
    policy: NumberPolicy,
) -> list[tuple[Fragment, str, float]]:
    """Tracklets whose pooled number reaches the frozen threshold."""
    output = []
    for piece in pieces:
        if piece.replay or piece.conflicted:
            continue
        number, mass = strongest(
            pooled(grouped.get(piece.identity, []), policy.min_support)
        )
        if number is not None and mass >= policy.threshold:
            output.append((piece, number, mass))
    return output


@dataclass(frozen=True)
class KitRead:
    """One confidently read tracklet, in the run's anonymous kit tags.

    Kit tags (``team_a``/``team_b``) are colour groups of this recording, not
    roster sides: which roster side wears which kit is decided separately, so a
    read keeps its kit and number until that orientation is known.
    """

    identity: str
    kit: str | None
    number: str
    mass: float


@dataclass(frozen=True)
class Sighting:
    """One observation of the kit orientation: a roster player seen in a kit.

    ``kind`` is ``confirmation`` (a reviewer named the view) or ``number`` (a
    number only one roster side wears was read on it); ``view`` and
    ``section`` say where, so the same view is never counted twice.
    """

    player_id: str
    kit: str
    kind: str
    view: str
    section: str = ""


@dataclass(frozen=True)
class Orientation:
    """Which roster side wears which kit, and the evidence that decided it.

    ``swapped`` is ``None`` while the evidence cannot tell (``state`` is then
    ``undecided`` or ``contradicted``); nothing that depends on the kit (a
    number on a kit-tagged view, an appearance name of one) may name a player
    then. ``outliers`` are players whose vote contradicts a decision.
    """

    swapped: bool | None
    source: str | None
    support: dict[str, int]
    state: str = "undecided"
    posterior: float | None = None
    outliers: tuple[str, ...] = ()

    @property
    def decided(self) -> bool:
        """Whether the roster sides are known to wear particular kits."""
        return self.swapped is not None

    def receipt(self) -> dict[str, Any]:
        """Return the orientation as published with a solve."""
        return {
            "swapped": self.swapped,
            "source": self.source,
            "state": self.state,
            "support": self.support,
            "posterior": self.posterior,
            "outliers": list(self.outliers),
        }


def kit_reads(reads: list[tuple[Fragment, str, float]]) -> list[KitRead]:
    """Keep confident reads in kit coordinates, independent of the roster."""
    return [
        KitRead(
            piece.identity,
            piece.team if piece.team in TEAMS else None,
            number,
            round(mass, 4),
        )
        for piece, number, mass in reads
    ]


def roster_side(kit: str, swapped: bool) -> str:
    """Return the roster side that wears ``kit`` under an orientation."""
    return SWAPPED[kit] if swapped else kit


def sightings(
    reads: Sequence[KitRead],
    roster: list[Any],
    confirmed: Sequence[tuple[str, str | None, str]] = (),
    *,
    section: str = "",
) -> list[Sighting]:
    """Return one section's sightings: at most one per view.

    ``confirmed`` holds ``(view, kit, player_id)`` per confirmation. A
    confirmation overrules its own view's read; a read counts only when
    exactly one roster side wears its number.
    """
    sides = {p.player_id: p.team for p in roster}
    slots = {
        (p.team, p.number): p.player_id
        for p in roster
        if p.number is not None and p.team in TEAMS
    }
    found: dict[str, Sighting] = {}
    for view, kit, player_id in confirmed:
        if kit in TEAMS and sides.get(player_id) in TEAMS:
            found[view] = Sighting(player_id, kit, CONFIRMATION, view, section)
    for read in reads:
        if read.kit not in TEAMS or read.identity in found:
            continue
        here = slots.get((read.kit, read.number))
        there = slots.get((SWAPPED[read.kit], read.number))
        if (here is None) != (there is None):
            player_id = here or there
            assert player_id is not None
            found[read.identity] = Sighting(
                player_id, read.kit, NUMBER, read.identity, section
            )
    return list(found.values())


def ballot(
    evidence: Iterable[Sighting], roster: list[Any]
) -> tuple[dict[str, str], dict[str, set[str]], int]:
    """One vote per player: the state its sightings show most often.

    Returns:
        Each voting player's state, the kinds of sightings behind it, and how
        many players abstained because they were seen in both kits equally.

    """
    sides = {p.player_id: p.team for p in roster if p.team in TEAMS}
    seen: dict[str, Counter[str]] = defaultdict(Counter)
    kinds: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for sighting in set(evidence):
        side = sides.get(sighting.player_id)
        if side is None or sighting.kit not in TEAMS:
            continue
        state = "kept" if sighting.kit == side else "swapped"
        seen[sighting.player_id][state] += 1
        kinds[sighting.player_id][state].add(sighting.kind)
    votes, behind, abstained = {}, {}, 0
    for player_id, counts in seen.items():
        if counts["kept"] == counts["swapped"]:
            abstained += 1
            continue
        state = "kept" if counts["kept"] > counts["swapped"] else "swapped"
        votes[player_id], behind[player_id] = state, kinds[player_id][state]
    return votes, behind, abstained


def tail(trials: int, errors: int, rate: float) -> float:
    """Probability of at least ``errors`` wrong votes among ``trials``."""
    return sum(
        math.comb(trials, k) * rate**k * (1 - rate) ** (trials - k)
        for k in range(errors, trials + 1)
    )


def upper_bound(wrong: int, trials: int, confidence: float) -> float:
    """One-sided exact (Clopper-Pearson) upper bound of an error rate."""
    low, high = wrong / trials, 1.0
    for _ in range(60):
        middle = (low + high) / 2
        if 1 - tail(trials, wrong + 1, middle) > 1 - confidence:
            low = middle
        else:
            high = middle
    return high


# The largest per-vote error the development measurement cannot exclude.
MAX_VOTE_ERROR = upper_bound(MEASURED_WRONG, MEASURED_VOTES, BOUND_CONFIDENCE)


def vote_error(agree: int, against: int) -> float:
    """Estimate a recording's per-vote error from measurement and own dissent.

    The posterior mean of a Jeffreys prior updated with the development votes
    and then with this recording's ``against`` dissenting of all its votes, never
    below ``OBSERVATION_ERROR``.
    """
    wrong = MEASURED_WRONG + 0.5 + against
    return max(OBSERVATION_ERROR, wrong / (MEASURED_VOTES + 1 + agree + against))


def orientation(evidence: Iterable[Sighting], roster: list[Any]) -> Orientation:
    """Decide whether the roster's sides wear the opposite kit tags.

    See the module docstring: one vote per player, a posterior at this
    recording's estimated vote error that needs at least two more agreeing
    players than dissenting ones, and contradictions no more frequent than any
    per-vote error the development measurement allows. Re-run this whenever
    any evidence of the recording changes.
    """
    votes, behind, abstained = ballot(evidence, roster)
    count = Counter(votes.values())
    lead = "swapped" if count["swapped"] > count["kept"] else "kept"
    agree, against = count[lead], count[SWAPPED_STATE[lead]]
    error = vote_error(agree, against)
    posterior = 1 / (1 + (error / (1 - error)) ** (agree - against))
    support = {
        "kept": count["kept"],
        "swapped": count["swapped"],
        "abstained": abstained,
    }
    if tail(agree + against, against, MAX_VOTE_ERROR) < CONTRADICTION_P:
        return Orientation(None, None, support, "contradicted", round(posterior, 6))
    if posterior < 1 - DECISION_RISK:
        return Orientation(None, None, support, "undecided", round(posterior, 6))
    automatic = any(
        NUMBER in behind[player_id]
        for player_id, state in votes.items()
        if state == lead
    )
    return Orientation(
        lead == "swapped",
        "evidence" if automatic else "human",
        support,
        "decided",
        round(posterior, 6),
        tuple(sorted(p for p, state in votes.items() if state != lead)),
    )


def poisson(count: int, rate: float) -> float:
    """Poisson probability of exactly ``count`` events at ``rate``."""
    if rate <= 0:
        return float(count == 0)
    return math.exp(count * math.log(rate) - rate - math.lgamma(count + 1))


def playing(count: int, full: float) -> float:
    """Likelihood of ``count`` anchors for a player who played an unknown share."""
    return max(0.0, 1 - sum(poisson(k, full) for k in range(count + 1))) / full


def absent(
    numbers: Sequence[Anchor], roster: list[Any], attested: set[str]
) -> set[str]:
    """Return players whose number anchors misreads alone explain.

    ``attested`` players were confirmed by a person and are always present.
    See the module docstring ("Presence").
    """
    slots = sum(p.number is not None for p in roster)
    total = len(numbers)
    if slots < MIN_SUPPORT or not total:
        return set()
    full = total / slots
    stray = NUMBER_READ_ERROR * total / (slots - 1)
    counts = Counter(a.player_id for a in numbers)
    return {
        player_id
        for player_id, count in counts.items()
        if player_id not in attested and playing(count, full) < poisson(count, stray)
    }


def oriented(roster: list[Any], swapped: bool) -> list[Any]:
    """Return the roster with kit tags exchanged when its sides were swapped."""
    if not swapped:
        return list(roster)
    return [type(p)(p.player_id, SWAPPED[p.team], p.number) for p in roster]


def overruled(
    anchor: Anchor,
    piece: Fragment,
    humans: Sequence[Anchor],
    found: Mapping[str, Fragment],
) -> bool:
    """Let a confirmation of this tracklet, or of its player at that time, win."""
    return any(
        human.identity == anchor.identity
        or (
            human.player_id == anchor.player_id
            and human.identity in found
            and overlap(found[human.identity], piece)
        )
        for human in humans
    )


def respecting(
    automatic: Sequence[Anchor], humans: Sequence[Anchor], pieces: list[Fragment]
) -> tuple[list[Anchor], int]:
    """Human confirmations override numbers; return the kept anchors and drops."""
    found = {piece.identity: piece for piece in pieces}
    kept = [
        a
        for a in automatic
        if a.identity in found and not overruled(a, found[a.identity], humans, found)
    ]
    return kept, len(automatic) - len(kept)


def anchors(
    pieces: list[Fragment],
    grouped: Mapping[str, list[dict[str, float]]],
    roster: list[Any],
    policy: NumberPolicy,
    *,
    swapped: bool | None = False,
) -> tuple[list[Anchor], dict[str, Any]]:
    """Name confidently read tracklets; see ``named``."""
    reads = kit_reads(confident(pieces, grouped, policy))
    accepted, receipt = named(pieces, reads, roster, swapped)
    return accepted, {
        "version": 2,
        "policy": {
            "threshold": policy.threshold,
            "min_support": policy.min_support,
            "provenance": policy.provenance,
        },
        **receipt,
    }


def named(
    pieces: list[Fragment],
    reads: Sequence[KitRead],
    roster: list[Any],
    swapped: bool | None,
) -> tuple[list[Anchor], dict[str, Any]]:
    """Turn kit reads into roster anchors under one kit orientation.

    The roster uses roster sides; anchors do too. While the orientation is
    unknown (``None``) a kit-tagged read names nobody: either side could be
    wearing that kit. A number read without a kit and worn on both sides is
    ambiguous and names nobody.
    Conflicting anchors are rejected together. Human confirmations are applied
    later with ``respecting`` so every answer re-checks them.
    """
    receipt: dict[str, Any] = {
        "confident_tracklets": len(reads),
        "rejected_not_in_roster": 0,
        "rejected_team_unknown": 0,
        "rejected_unoriented": 0,
    }
    found = {piece.identity: piece for piece in pieces}
    by_slot = {(p.team, p.number): p for p in roster if p.number is not None}
    by_number: dict[str, list[Any]] = defaultdict(list)
    for player in roster:
        if player.number is not None:
            by_number[player.number].append(player)
    candidates: list[tuple[Fragment, Anchor]] = []
    for read in reads:
        piece = found.get(read.identity)
        if piece is None:
            continue
        player, problem = wearer(read, swapped, by_slot, by_number)
        if player is None:
            receipt[problem] += 1
            continue
        candidates.append((
            piece,
            Anchor(
                piece.identity,
                player.player_id,
                player.team,
                player.number,
                source=SOURCE,
                confidence=read.mass,
            ),
        ))
    rejected: set[str] = set()
    for (first, a), (second, b) in combinations(candidates, 2):
        if a.player_id == b.player_id and overlap(first, second):
            rejected.update((a.identity, b.identity))
    accepted = [a for _, a in candidates if a.identity not in rejected]
    receipt["rejected_conflict"] = len(rejected)
    receipt["accepted"] = len(accepted)
    receipt["players"] = len({a.player_id for a in accepted})
    return accepted, receipt


def wearer(
    read: KitRead,
    swapped: bool | None,
    by_slot: Mapping[tuple[str, str], Any],
    by_number: Mapping[str, list[Any]],
) -> tuple[Any, str]:
    """Return the roster player a read names, or ``None`` and the receipt key."""
    if read.kit in TEAMS:
        if swapped is None:
            return None, "rejected_unoriented"
        player = by_slot.get((roster_side(read.kit, swapped), read.number))
        return player, "rejected_not_in_roster"
    # Without a kit tag only a number worn by one roster player names it.
    options = by_number.get(read.number, [])
    if len(options) > 1:
        return None, "rejected_team_unknown"
    return (options[0] if options else None), "rejected_not_in_roster"
