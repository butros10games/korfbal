"""Regularized Poisson regression with a joint Laplace posterior approximation.

Independent baselines per season/format context, partially pooled class, poule,
attack and defence effects. Proper zero-centred Gaussian priors anchor otherwise
confounded levels. Hyperparameters are fixed before validation, not tuned on it.
NumPy/SciPy are development/offline dependencies, absent from the serving path.
"""

from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import math

import numpy as np
from scipy.linalg import cholesky, solve_triangular
from scipy.optimize import minimize

from apps.competition.domain.score_forecast import (
    DRAW_COUNT,
    PRIOR_SD,
    VERSION,
    context_key,
    timestamp,
)


MAX_SCORE = 150
MAX_COEFFICIENTS = 2500
MIN_CONTEXT_MATCHES = 10


def snapshot(rows: list[dict], cutoff: datetime) -> list[dict]:
    """Replay the latest observed score revision strictly before the training cutoff."""
    selected = []
    for row in rows:
        if timestamp(row["starts_at"]) >= cutoff:
            continue
        revisions = [
            r for r in row["revisions"] if timestamp(r["observed_at"]) < cutoff
        ]
        if not revisions:
            continue
        result = max(
            revisions, key=lambda r: (timestamp(r["observed_at"]), r["revision"])
        )
        if result["status"] != "FINAL" or result["automatic_result"]:
            continue
        scores = [result["home_score"], result["away_score"]]
        if any(
            type(score) is not int or not 0 <= score <= MAX_SCORE for score in scores
        ):
            continue
        # The feed has no verified-zero marker. Quarantine 0-0 rather than
        # silently treating scheduled placeholders as completed matches.
        if scores == [0, 0]:
            continue
        if (
            not row.get("duration_observed_at")
            or timestamp(row["duration_observed_at"]) >= cutoff
        ):
            continue
        selected.append({**row, "home_score": scores[0], "away_score": scores[1]})
    return selected


def fit_group(rows: list[dict], seed: int) -> dict:
    """Fit a context and retain correlated coefficient draws, including uncertainty.

    Raises:
        ValueError: Context is too large or numerical optimization fails.

    """
    identities = {
        "class": sorted({row["class"] for row in rows}),
        "pool": sorted({row["pool"] for row in rows}),
        "attack": sorted({row[side] for row in rows for side in ("home", "away")}),
        "defence": sorted({row[side] for row in rows for side in ("home", "away")}),
    }
    columns = [
        (kind, identity) for kind, values in identities.items() for identity in values
    ]
    lookup = {key: i + 2 for i, key in enumerate(columns)}
    width = len(columns) + 2
    if width > MAX_COEFFICIENTS:
        raise ValueError(
            "Context exceeds dense Laplace fitting limit (2500 coefficients)"
        )
    design = np.zeros((len(rows) * 2, width))
    outcomes, offsets = [], []
    for index, row in enumerate(rows):
        for side in range(2):
            n = 2 * index + side
            own, opponent = ("home", "away") if side == 0 else ("away", "home")
            design[n, 0], design[n, 1] = 1, int(side == 0)
            for kind in ("class", "pool"):
                design[n, lookup[kind, row[kind]]] = 1
            design[n, lookup["attack", row[own]]] = 1
            design[n, lookup["defence", row[opponent]]] = -1
            outcomes.append(row[f"{own}_score"])
            offsets.append(math.log(row["duration"] / 60))
    y, offset = np.asarray(outcomes), np.asarray(offsets)
    centre = np.zeros(width)
    centre[0] = math.log(10)
    precision = np.array(
        [1 / 1.5**2, 1 / 0.2**2] + [1 / PRIOR_SD[kind] ** 2 for kind, _ in columns]
    )

    def objective(parameters: np.ndarray) -> tuple:
        eta = design @ parameters + offset
        mu = np.exp(eta)
        delta = parameters - centre
        loss = np.sum(mu - y * eta) + np.sum(precision * delta**2) / 2
        gradient = design.T @ (mu - y) + precision * delta
        return loss, gradient

    result = minimize(
        objective,
        centre,
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": 1000, "ftol": 1e-12, "gtol": 1e-6},
    )
    if not result.success:
        raise ValueError(f"Score model did not converge: {result.message}")
    mu = np.exp(design @ result.x + offset)
    hessian = design.T @ (mu[:, None] * design) + np.diag(precision)
    factor = cholesky(hessian, lower=True)
    rng = np.random.default_rng(seed)
    samples = result.x[:, None] + solve_triangular(
        factor.T, rng.standard_normal((width, DRAW_COUNT)), lower=False
    )
    return {
        "intercept": samples[0].tolist(),
        "home_advantage": samples[1].tolist(),
        "effects": {
            kind: {
                identity: samples[lookup[kind, identity]].tolist()
                for identity in values
            }
            for kind, values in identities.items()
        },
        "matches": len(rows),
        "pool_matches": dict(Counter(row["pool"] for row in rows)),
        "team_matches": dict(
            Counter(row[side] for row in rows for side in ("home", "away"))
        ),
    }


def fit(rows: list[dict], cutoff: datetime, *, seed: int = 2026) -> dict:
    """Freeze an artifact; insufficient contexts stay unsupported."""
    groups: dict[str, list[dict]] = defaultdict(list)
    training = snapshot(rows, cutoff)
    for row in training:
        groups[context_key(row)].append(row)
    return {
        "version": VERSION,
        "training_cutoff": cutoff.isoformat(),
        "available_from": cutoff.isoformat(),
        "method": "joint-laplace-poisson",
        "reference_minutes": 60,
        "draw_count": DRAW_COUNT,
        "approved": False,
        "contexts": {
            key: fit_group(
                group, seed + int.from_bytes(sha256(key.encode()).digest()[:4])
            )
            for key, group in sorted(groups.items())
            if len(group) >= MIN_CONTEXT_MATCHES
        },
        "training_matches": len(training),
        "priors": PRIOR_SD,
    }
