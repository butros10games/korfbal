"""Dependency-free posterior predictive score distributions shared by fit and serve."""

from datetime import datetime
from hashlib import sha256
import math
from statistics import NormalDist
from typing import Any


VERSION = "hierarchical-poisson-v1"
DRAW_COUNT = 64
MAX_RATE = 250
MAX_DURATION = 120
CONTEXT_FIELDS = (
    "season",
    "discipline",
    "phase",
    "gender",
    "category",
    "age_group",
    "colour",
    "playing_format",
    "team_kind",
)
PRIOR_SD = {"class": 0.3, "pool": 0.25, "attack": 0.4, "defence": 0.4}


def context_key(row: dict) -> str:
    """Never share scoring baselines across incompatible formats or seasons."""
    return "|".join(str(row[field]) for field in CONTEXT_FIELDS)


def timestamp(value: str) -> datetime:
    """Require explicit timezone information for all knowledge boundaries.

    Raises:
        ValueError: Timestamp is invalid or lacks a timezone.

    """
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Forecast timestamps must include a timezone")
    return result


def poisson(mean: float) -> list[float]:
    """Retain negligible omitted tail mass, including very low scoring games.

    Raises:
        ValueError: Rate is outside the supported numeric range.

    """
    if not math.isfinite(mean) or not 0 <= mean <= MAX_RATE:
        raise ValueError("Unsupported scoring rate")
    values = [math.exp(-mean)]
    for goals in range(1, math.ceil(mean + 12 * math.sqrt(mean + 1) + 20) + 1):
        values.append(values[-1] * mean / goals)
    total = sum(values)
    return [value / total for value in values]


def rate_draws(artifact: dict, row: dict) -> list[list[float]] | None:
    """Integrate unknown teams/poules over their priors, never fit during serving."""
    group = artifact["contexts"].get(context_key(row))
    if group is None:
        return None
    duration = row["duration"]
    if not isinstance(duration, (int, float)) or not 0 < duration <= MAX_DURATION:
        return None
    effects: dict[tuple[str, str], list[float]] = {}

    def effect(kind: str, identity: str) -> list[float]:
        key = (kind, identity)
        if key not in effects:
            known = group["effects"][kind].get(identity)
            if known is None:
                # Stable across requests, independent across unseen identities and
                # effect types, with the same prior used by the offline fitter.
                seed = sha256(
                    f"{artifact['version']}|{context_key(row)}|{kind}|{identity}".encode()
                ).digest()
                known = [
                    NormalDist(0, PRIOR_SD[kind]).inv_cdf(
                        (
                            int.from_bytes(sha256(seed + str(i).encode()).digest()[:6])
                            + 0.5
                        )
                        / 2**48
                    )
                    for i in range(len(group["intercept"]))
                ]
            effects[key] = known
        return effects[key]

    shared = [
        intercept + cls + pool + math.log(duration / 60)
        for intercept, cls, pool in zip(
            group["intercept"],
            effect("class", row["class"]),
            effect("pool", row["pool"]),
            strict=True,
        )
    ]
    home_attack, away_attack = (
        effect("attack", row["home"]),
        effect("attack", row["away"]),
    )
    home_defence, away_defence = (
        effect("defence", row["home"]),
        effect("defence", row["away"]),
    )
    draws = [
        [math.exp(pace + bias + ha - ad), math.exp(pace + aa - hd)]
        for pace, bias, ha, aa, hd, ad in zip(
            shared,
            group["home_advantage"],
            home_attack,
            away_attack,
            home_defence,
            away_defence,
            strict=True,
        )
    ]
    if any(
        not math.isfinite(rate) or not 0 < rate <= MAX_RATE
        for draw in draws
        for rate in draw
    ):
        return None
    return draws


def summarize(
    draws: list[list[float]], outcome_weights: dict[str, float] | None = None
) -> dict[str, Any]:
    """Derive every kickoff summary from the same mixture of joint score laws."""
    pairs = [(poisson(home), poisson(away)) for home, away in draws]
    size_h, size_a = max(len(h) for h, _ in pairs), max(len(a) for _, a in pairs)
    joint = [[0.0] * size_a for _ in range(size_h)]
    for home, away in pairs:
        for h, ph in enumerate(home):
            for a, pa in enumerate(away):
                joint[h][a] += ph * pa / len(draws)
    if outcome_weights is not None:
        for h, values in enumerate(joint):
            for a in range(len(values)):
                outcome = "home" if h > a else "away" if h < a else "draw"
                values[a] *= outcome_weights[outcome]
        mass = sum(sum(values) for values in joint)
        joint = [[p / mass for p in values] for values in joint]
    home = [sum(values) for values in joint]
    away = [sum(values[a] for values in joint) for a in range(size_a)]

    def quantile(values: list[float], probability: float) -> int:
        cumulative = 0.0
        for index, value in enumerate(values):
            cumulative += value
            if cumulative >= probability:
                return index
        return len(values) - 1

    likely = max(
        ((h, a) for h in range(size_h) for a in range(size_a)),
        key=lambda pair: joint[pair[0]][pair[1]],
    )
    return {
        "expected_goals": {
            "home": (
                sum(i * p for i, p in enumerate(home))
                if outcome_weights
                else sum(h for h, _ in draws) / len(draws)
            ),
            "away": (
                sum(i * p for i, p in enumerate(away))
                if outcome_weights
                else sum(a for _, a in draws) / len(draws)
            ),
        },
        "most_likely_score": {"home": likely[0], "away": likely[1]},
        "interval_80": {
            "home": [quantile(home, 0.1), quantile(home, 0.9)],
            "away": [quantile(away, 0.1), quantile(away, 0.9)],
        },
        "probabilities": {
            "home": sum(
                value
                for h, values in enumerate(joint)
                for a, value in enumerate(values)
                if h > a
            ),
            "draw": sum(joint[h][h] for h in range(min(size_h, size_a))),
            "away": sum(
                value
                for h, values in enumerate(joint)
                for a, value in enumerate(values)
                if h < a
            ),
        },
        "opponent_target_75": {
            "home": quantile(away, 0.75) + 1,
            "away": quantile(home, 0.75) + 1,
        },
    }
