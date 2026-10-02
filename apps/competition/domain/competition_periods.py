"""Decide a poule's competition period from the whole poule, never one fixture.

Live and historical imports share these rules. A poule is one competition, so
its period follows from all of its fixtures (and explicit label evidence), not
from the calendar month of an individual, possibly rescheduled, fixture.

Outdoor play pauses for the indoor season; 1 January always lies inside that
break, so it separates *fixture dates* of the two outdoor halves. It is not
used as a boundary for indoor competition parts, which KNKV splits in
mid-January: those parts are recognised from one team's separately graded,
non-overlapping indoor poules instead.

KNKV references: https://www.knkv.nl/kennisbank/competitiehandboek/ (indoor
four-player youth parts) and Reglement van Wedstrijden art. 21 (continuous
outdoor competitions across the winter break).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from operator import itemgetter


AUTUMN = "autumn"
SPRING = "spring"
FULL_SEASON = "full_season"
INDOOR = "indoor"

# One fixture on the other side of the break is a rescheduled fixture of a
# single-half poule, provided the poule clearly belongs to its majority half.
RESCHEDULED_TOLERANCE = 1
MINIMUM_MAJORITY = 4
# A team needs at least two poules for them to be consecutive parts.
MINIMUM_SEQUENCE = 2


@dataclass(frozen=True)
class PhaseDecision:
    """A period decision, or None with the reason it could not be decided."""

    phase: str | None
    evidence: dict[str, object] = field(default_factory=dict)


def outdoor_phase(
    days: Iterable[date], edition: int, label_phase: str | None = None
) -> PhaseDecision:
    """Decide whether an outdoor poule plays autumn, spring or both halves.

    Returns:
        The decision with its counted evidence.

    """
    turn = date(edition + 1, 1, 1)
    autumn = spring = 0
    for day in days:
        if day < turn:
            autumn += 1
        else:
            spring += 1
    phase, reason = _phase_from_counts(autumn, spring, label_phase)
    evidence: dict[str, object] = {"autumn": autumn, "spring": spring, "reason": reason}
    if phase in {AUTUMN, SPRING}:
        evidence["rescheduled"] = spring if phase == AUTUMN else autumn
    return PhaseDecision(phase, evidence)


def _phase_from_counts(
    autumn: int, spring: int, label_phase: str | None
) -> tuple[str | None, str]:
    """Return the phase and the reason for it from fixture counts per half."""
    if label_phase in {AUTUMN, SPRING}:
        return label_phase, "label"
    if not autumn and not spring:
        return None, "no_fixtures"
    if not spring or not autumn:
        return (AUTUMN if autumn else SPRING), "fixtures"
    minority, majority = sorted((autumn, spring))
    if minority > RESCHEDULED_TOLERANCE:
        return FULL_SEASON, "both_halves"
    if majority >= MINIMUM_MAJORITY:
        return (AUTUMN if autumn > spring else SPRING), "rescheduled"
    return None, "ambiguous_halves"


def indoor_parts(
    team_pools: Iterable[Iterable[tuple[str, date, date]]],
) -> dict[str, PhaseDecision]:
    """Decide independent indoor competition parts from teams' poule sequences.

    ``team_pools`` holds each team's indoor poules as ``(pool, first, last)``
    fixture days. A team whose poules do not overlap in time played them as
    consecutive, separately graded parts. A poule gets a part only when every
    team that evidences it agrees; poules a team played alone stay unnumbered.

    Returns:
        A decision per poule seen in a multi-poule sequence.

    """
    votes: dict[str, set[int]] = defaultdict(set)
    overlapping: set[str] = set()
    for pools in team_pools:
        ordered = sorted(pools, key=itemgetter(1, 2, 0))
        if len(ordered) < MINIMUM_SEQUENCE:
            continue
        if any(later[1] <= earlier[2] for earlier, later in pairwise(ordered)):
            overlapping.update(pool for pool, _, _ in ordered)
            continue
        for number, (pool, _, _) in enumerate(ordered, start=1):
            votes[pool].add(number)
    decisions: dict[str, PhaseDecision] = {}
    for pool in sorted(set(votes) | overlapping):
        numbers = votes.get(pool, set())
        if len(numbers) == 1 and pool not in overlapping:
            decisions[pool] = PhaseDecision(
                INDOOR, {"part": numbers.pop(), "reason": "team_sequence"}
            )
        else:
            decisions[pool] = PhaseDecision(
                INDOOR,
                {
                    "part": None,
                    "reason": "conflicting_sequence",
                    "votes": sorted(numbers),
                },
            )
    return decisions
