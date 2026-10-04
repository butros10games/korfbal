"""Shirt-number evidence with explicit abstention and bounded episode voting."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import math


UNKNOWN = "unknown"
NUMBERS = tuple(str(n) for n in range(100))
MAX_EPISODES = 16
EPISODE_GAP = 1.0
TEMPERATURE = 1.25
PROBABILITY_TOLERANCE = 1e-5
MIN_LEGIBILITY = 0.5
MIN_SUPPORT = 3
MAX_READINGS = 32


def normalized(values: Mapping[str, float]) -> dict[str, float]:
    """Validate probabilities; missing number mass remains unknown.

    Raises:
        ValueError: Evidence contains invalid keys or nonfinite probabilities.

    """
    if any(k not in {*NUMBERS, UNKNOWN} for k in values):
        raise ValueError("Invalid shirt-number evidence key")
    if any(not math.isfinite(v) or v < 0 for v in values.values()):
        raise ValueError("Invalid shirt-number probability")
    total = sum(values.values())
    if total > 1 + PROBABILITY_TOLERANCE:
        raise ValueError("Shirt-number probabilities exceed one")
    result = dict(values)
    result[UNKNOWN] = result.get(UNKNOWN, 0.0) + max(0.0, 1 - total)
    return {k: v / sum(result.values()) for k, v in result.items()}


def restricted(values: Mapping[str, float], roster: Sequence[str]) -> dict[str, float]:
    """Move out-of-roster mass to unknown; a roster cannot create certainty."""
    distribution = normalized(values)
    allowed = set(roster).intersection(NUMBERS)
    result = {k: v for k, v in distribution.items() if k in allowed}
    result[UNKNOWN] = 1 - sum(result.values())
    return result


def tempered(values: Mapping[str, float], temperature: float) -> dict[str, float]:
    """Flatten conditional digit probabilities while preserving legibility.

    Raises:
        ValueError: Temperature sharpens confidence or is nonfinite.

    """
    if not math.isfinite(temperature) or temperature < 1:
        raise ValueError("Number-evidence temperature must be at least one")
    distribution = normalized(values)
    mass = 1 - distribution[UNKNOWN]
    weights = {
        k: v ** (1 / temperature)
        for k, v in distribution.items()
        if k != UNKNOWN and v > 0
    }
    total = sum(weights.values())
    return {
        **{k: mass * v / total for k, v in weights.items()},
        UNKNOWN: distribution[UNKNOWN],
    }


def best(values: Mapping[str, float]) -> tuple[str | None, float]:
    """Return the strongest number's joint probability, retaining abstention."""
    candidates = [(v, k) for k, v in values.items() if k != UNKNOWN]
    if not candidates:
        return None, 0.0
    confidence, value = max(candidates)
    return value, confidence


@dataclass
class EpisodeVotes:
    """Keep one strongest reading per continuous visibility episode.

    Neighboring video frames never increase the number of independent votes.
    Camera changes, an absence, or a hidden shirt end an episode. The newest
    sixteen episode peaks are averaged and tempered, never multiplied.
    """

    temperature: float = TEMPERATURE
    peaks: OrderedDict[int, dict[str, float]] = field(default_factory=OrderedDict)
    episode: int = -1
    last_time: float = -math.inf
    camera: int | None = None
    visible: bool = False

    def observe(
        self, values: Mapping[str, float], time_seconds: float, camera: int | None
    ) -> dict[str, float]:
        """Add a crop and return the current tracklet distribution."""
        distribution = normalized(values)
        visible = 1 - distribution[UNKNOWN] >= MIN_LEGIBILITY
        if visible:
            if (
                not self.visible
                or camera != self.camera
                or time_seconds - self.last_time > EPISODE_GAP
            ):
                self.episode += 1
            previous = self.peaks.get(self.episode, {UNKNOWN: 1.0})
            if best(distribution)[1] > best(previous)[1]:
                self.peaks[self.episode] = distribution
            while len(self.peaks) > MAX_EPISODES:
                self.peaks.popitem(last=False)
        self.visible, self.last_time, self.camera = visible, time_seconds, camera
        return self.distribution()

    def distribution(self) -> dict[str, float]:
        """Average episode peaks; an entirely hidden track remains unknown."""
        if not self.peaks:
            return {UNKNOWN: 1.0}
        count = len(self.peaks)
        votes = {
            key: sum(p.get(key, 0.0) for p in self.peaks.values()) / count
            for key in (*NUMBERS, UNKNOWN)
        }
        return tempered({k: v for k, v in votes.items() if v > 0}, self.temperature)


@dataclass
class AgreementVotes:
    """Pool readable 0.4 s readings; one confident view can never decide alone.

    Every readable crop distribution is added; the tracklet mass of a number is
    the summed probability divided by ``max(readings, min_support)``. A single
    strong (possibly spurious) view contributes at most ``1 / min_support``;
    conflicting views dilute each other instead of the strongest peak winning.
    Hidden crops do not vote. Episode bookkeeping matches ``EpisodeVotes`` so the
    emitted fields stay compatible; ``peaks`` counts readable readings per episode.
    """

    min_support: int = MIN_SUPPORT
    readings: list[dict[str, float]] = field(default_factory=list)
    peaks: OrderedDict[int, int] = field(default_factory=OrderedDict)
    episode: int = -1
    last_time: float = -math.inf
    camera: int | None = None
    visible: bool = False

    def observe(
        self, values: Mapping[str, float], time_seconds: float, camera: int | None
    ) -> dict[str, float]:
        """Add a crop and return the current tracklet distribution."""
        distribution = normalized(values)
        visible = 1 - distribution[UNKNOWN] >= MIN_LEGIBILITY
        if visible:
            if (
                not self.visible
                or camera != self.camera
                or time_seconds - self.last_time > EPISODE_GAP
            ):
                self.episode += 1
            self.peaks[self.episode] = self.peaks.get(self.episode, 0) + 1
            while len(self.peaks) > MAX_EPISODES:
                self.peaks.popitem(last=False)
            self.readings.append(distribution)
            del self.readings[:-MAX_READINGS]
        self.visible, self.last_time, self.camera = visible, time_seconds, camera
        return self.distribution()

    def distribution(self) -> dict[str, float]:
        """Sum readable evidence over at least ``min_support`` readings."""
        if not self.readings:
            return {UNKNOWN: 1.0}
        count = max(len(self.readings), self.min_support)
        votes = {
            key: sum(r.get(key, 0.0) for r in self.readings) / count for key in NUMBERS
        }
        votes = {k: v for k, v in votes.items() if v > 0}
        return normalized(votes)
