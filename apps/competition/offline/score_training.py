"""Regularized Poisson regression with a joint Laplace posterior approximation.

Independent baselines per season/format context, partially pooled class, poule,
attack and defence effects. Proper zero-centred Gaussian priors anchor otherwise
confounded levels. Hyperparameters are fixed before validation, not tuned on it.
NumPy/SciPy are development/offline dependencies, absent from the serving path.

Optionally, earlier seasons start contexts that lack the ten-result minimum from
the scoring pace and class effects their format had before, with team uncertainty
from the observed between-team spread, so they are forecast before their first
result. Established contexts are fitted exactly as without earlier seasons: in a
replay of the 2026 autumn start, earlier priors made their W/D/L Brier score
significantly worse, and carrying earlier team strength over (damped or in full)
never improved on the pace alone.
"""

from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import math
from statistics import median

import numpy as np
from scipy.linalg import cholesky, solve_triangular
from scipy.optimize import minimize
from scipy.sparse import csr_matrix

from apps.competition.domain.score_forecast import (
    DRAW_COUNT,
    PACE_FIELDS,
    PRIOR_SD,
    VERSION,
    context_key,
    timestamp,
)
from apps.competition.domain.score_observations import result_snapshot, snapshot


MAX_COEFFICIENTS = 2500
MIN_CONTEXT_MATCHES = 10
PACE_PRIOR_SD = 0.3
HOME_PRIOR_SD = 0.05
MIN_TEAM_SD = 0.1
OTHER_DISCIPLINE = {"indoor": "outdoor", "outdoor": "indoor"}
DEFAULT_PACE = math.log(10)
DEFAULT_PACE_SD = 1.5
DEFAULT_HOME_SD = 0.2


def pace_key(row: dict) -> str:
    """Scoring-format key shared across seasons and phases of one discipline."""
    return "|".join(str(row[field]) for field in PACE_FIELDS)


def covers(general: str, specific: str) -> bool:
    """Check an earlier format, its unknowns as wildcards, against a known one."""
    return all(
        left in {right, "unknown"}
        for left, right in zip(general.split("|"), specific.split("|"), strict=True)
    )


def target_keys(row: dict) -> list[str]:
    """List this context's formats; a gender-pending poule may use the mixed one."""
    keys = [pace_key(row)]
    if row["gender"] == "unknown":
        keys.append(pace_key({**row, "gender": "mixed"}))
    return keys


def pace_lookup(prior: dict, row: dict) -> str | None:
    """Prefer the most specific earlier format compatible with this context."""
    candidates = [
        key
        for key in prior["pace"]
        if any(covers(key, target) for target in target_keys(row))
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda key: (key.count("unknown"), key))


def imputed_durations(keys: set[str], durations: dict[str, float]) -> dict[str, float]:
    """Typical playing time per earlier format, from compatible current formats.

    The same format in the other discipline is a fallback: KNKV playing times
    depend on age group, colour and size rather than on indoor or outdoor.
    """
    imputed = {}
    for key in keys:
        discipline, rest = key.split("|", 1)
        for candidate in (key, f"{OTHER_DISCIPLINE[discipline]}|{rest}"):
            values = [
                minutes
                for known, minutes in durations.items()
                if covers(candidate, known)
            ]
            if values:
                imputed[key] = median(values)
                break
    return imputed


def history_prior(rows: list[dict], durations: dict[str, float]) -> dict | None:
    """Fit earlier results jointly by pace key, class and cross-season team.

    Earlier editions lack observed playing times; each result is scaled by the
    typical duration its scoring format has, and formats without one are skipped.
    """
    usable = []
    imputed = imputed_durations({pace_key(row) for row in rows}, durations)
    for row in rows:
        duration = row.get("duration") or imputed.get(pace_key(row))
        if duration and row.get("home_identity") and row.get("away_identity"):
            usable.append((row, duration))
    if not usable:
        return None
    design, columns, y, offset = history_design(usable)
    scale = {
        "home": DEFAULT_HOME_SD,
        "pace": DEFAULT_PACE_SD,
        "class": PRIOR_SD["class"],
        "attack": PRIOR_SD["attack"],
        "defence": PRIOR_SD["defence"],
    }
    centre = np.array([DEFAULT_PACE if kind == "pace" else 0.0 for kind, _ in columns])
    precision = np.array([1 / scale[kind] ** 2 for kind, _ in columns])
    estimate = posterior_mode(design, y, offset, (centre, precision), 2000)
    prior: dict = {
        "home": float(estimate[0]),
        "matches": len(usable),
        **{kind: {} for kind in ("pace", "class", "attack", "defence")},
    }
    for position, (kind, identity) in enumerate(columns):
        if kind != "home":
            prior[kind][identity] = float(estimate[position])
    prior["spread"] = team_spread(prior, usable)
    return prior


def team_spread(prior: dict, usable: list[tuple[dict, float]]) -> dict[str, float]:
    """Observed between-team spread, from teams with enough earlier results.

    A new phase starts every team from this spread rather than the wide default:
    integrating rates over the default would inflate expected goals by about a
    third when no team in a context has a result yet.
    """
    appearances = Counter(
        row[f"{side}_identity"] for row, _ in usable for side in ("home", "away")
    )
    settled = [
        team for team, count in appearances.items() if count >= MIN_CONTEXT_MATCHES
    ]
    spread = {}
    for kind in ("attack", "defence"):
        values = [prior[kind][team] ** 2 for team in settled]
        observed = math.sqrt(sum(values) / len(values)) if values else PRIOR_SD[kind]
        spread[kind] = min(PRIOR_SD[kind], max(observed, MIN_TEAM_SD))
    return spread


def history_design(
    usable: list[tuple[dict, float]],
) -> tuple[csr_matrix, list[tuple[str, str]], np.ndarray, np.ndarray]:
    """Sparse two-rows-per-match design; earlier teams are linked across seasons."""
    columns: dict[tuple[str, str], int] = {("home", ""): 0}
    rows_index, cols_index, values, outcomes, offsets = [], [], [], [], []
    for index, (row, duration) in enumerate(usable):
        pace = pace_key(row)
        for side, (own, opponent) in enumerate((("home", "away"), ("away", "home"))):
            entries = [
                (("pace", pace), 1.0),
                (("class", f"{pace}|{row['class_code']}"), 1.0),
                (("attack", row[f"{own}_identity"]), 1.0),
                (("defence", row[f"{opponent}_identity"]), -1.0),
            ]
            if side == 0:
                entries.append((("home", ""), 1.0))
            for key, value in entries:
                rows_index.append(2 * index + side)
                cols_index.append(columns.setdefault(key, len(columns)))
                values.append(value)
            outcomes.append(row[f"{own}_score"])
            offsets.append(math.log(duration / 60))
    design = csr_matrix(
        (values, (rows_index, cols_index)), shape=(2 * len(usable), len(columns))
    )
    return design, list(columns), np.asarray(outcomes, float), np.asarray(offsets)


def posterior_mode(
    design: np.ndarray | csr_matrix,
    y: np.ndarray,
    offset: np.ndarray,
    prior: tuple[np.ndarray, np.ndarray],
    iterations: int,
) -> np.ndarray:
    """Maximize the Poisson likelihood under independent Gaussian priors.

    Raises:
        ValueError: Numerical optimization fails.

    """
    centre, precision = prior

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
        options={"maxiter": iterations, "ftol": 1e-12, "gtol": 1e-6},
    )
    if not result.success:
        raise ValueError(f"Score model did not converge: {result.message}")
    return result.x


def format_durations(rows: list[dict], cutoff: datetime) -> dict[str, float]:
    """Typical playing time per scoring format, from durations known by the cutoff."""
    observed: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        known = row.get("duration_observed_at")
        if row.get("duration") and known and timestamp(known) < cutoff:
            observed[pace_key(row)].append(row["duration"])
    return {key: median(values) for key, values in observed.items()}


def context_priors(
    rows: list[dict],
    training: list[dict],
    prior_rows: list[dict],
    cutoff: datetime,
) -> dict[str, dict]:
    """Centre contexts lacking the training minimum from data known by the cutoff.

    Excluding the context's own edition prevents the season's results from also
    entering as their own prior; earlier phases and editions remain available.
    """
    counts = Counter(context_key(row) for row in training)
    earlier = result_snapshot(prior_rows, cutoff)
    durations = format_durations([*rows, *prior_rows], cutoff)
    by_edition: dict[str, dict | None] = {}
    priors = {}
    for row in rows:
        key = context_key(row)
        if key in priors or counts[key] >= MIN_CONTEXT_MATCHES:
            continue
        edition = row["season"]
        if edition not in by_edition:
            by_edition[edition] = history_prior(
                [item for item in [*earlier, *training] if item["season"] != edition],
                durations,
            )
        prior = by_edition[edition]
        matched = pace_lookup(prior, row) if prior else None
        if prior is None or matched is None:
            continue
        priors[key] = {
            "pace": prior["pace"][matched],
            "home": prior["home"],
            "class": class_priors(prior, matched),
            "spread": prior["spread"],
            "pace_key": matched,
            "history_matches": prior["matches"],
        }
    return priors


def class_priors(prior: dict, pace: str) -> dict[str, float]:
    """Earlier class effects within one scoring format, keyed by class code."""
    classes = {}
    for name, value in prior["class"].items():
        key, code = name.rsplit("|", 1)
        if key == pace:
            classes[code] = value
    return classes


def fit_group(
    rows: list[dict],
    seed: int,
    prior: dict | None = None,
    scheduled: list[dict] | None = None,
) -> dict:
    """Fit a context and retain correlated coefficient draws, including uncertainty.

    Raises:
        ValueError: Context is too large or numerical optimization fails.

    """
    members = [*rows, *(scheduled or [])]
    identities = {
        "class": sorted({row["class"] for row in members}),
        "pool": sorted({row["pool"] for row in members}),
        "attack": sorted({row[side] for row in members for side in ("home", "away")}),
        "defence": sorted({row[side] for row in members for side in ("home", "away")}),
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
    centre[0] = DEFAULT_PACE
    precision = np.array(
        [1 / DEFAULT_PACE_SD**2, 1 / DEFAULT_HOME_SD**2]
        + [1 / PRIOR_SD[kind] ** 2 for kind, _ in columns]
    )
    if prior is not None:
        centre_from(prior, members, lookup, centre, precision)
    estimate = posterior_mode(design, y, offset, (centre, precision), 1000)
    mu = np.exp(design @ estimate + offset)
    hessian = design.T @ (mu[:, None] * design) + np.diag(precision)
    factor = cholesky(hessian, lower=True)
    rng = np.random.default_rng(seed)
    samples = estimate[:, None] + solve_triangular(
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


def centre_from(
    prior: dict,
    members: list[dict],
    lookup: dict[tuple[str, str], int],
    centre: np.ndarray,
    precision: np.ndarray,
) -> None:
    """Move pace and class priors to their earlier estimates; widen teams by spread."""
    centre[0], precision[0] = prior["pace"], 1 / PACE_PRIOR_SD**2
    centre[1], precision[1] = prior["home"], 1 / HOME_PRIOR_SD**2
    for (kind, _), position in lookup.items():
        if kind in prior["spread"]:
            precision[position] = 1 / prior["spread"][kind] ** 2
    for row in members:
        code = prior["class"].get(row["class_code"])
        if code is not None:
            centre[lookup["class", row["class"]]] = code


def fit(
    rows: list[dict],
    cutoff: datetime,
    *,
    seed: int = 2026,
    prior_rows: list[dict] | None = None,
) -> dict:
    """Freeze an artifact; insufficient contexts without a pace prior stay unsupported.

    ``prior_rows`` holds earlier seasons. Fixtures not yet played still enter a
    context with a pace prior, so its teams carry the observed spread when served.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    training = snapshot(rows, cutoff)
    for row in training:
        groups[context_key(row)].append(row)
    priors = (
        context_priors(rows, training, prior_rows, cutoff)
        if prior_rows is not None
        else {}
    )
    scheduled: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if context_key(row) in priors:
            scheduled[context_key(row)].append(row)
    keys = {key for key, group in groups.items() if len(group) >= MIN_CONTEXT_MATCHES}
    keys |= set(priors)
    artifact = {
        "version": VERSION,
        "training_cutoff": cutoff.isoformat(),
        "available_from": cutoff.isoformat(),
        "method": "joint-laplace-poisson",
        "reference_minutes": 60,
        "draw_count": DRAW_COUNT,
        "approved": False,
        "contexts": {
            key: fit_group(
                groups[key],
                seed + int.from_bytes(sha256(key.encode()).digest()[:4]),
                priors.get(key),
                scheduled[key],
            )
            for key in sorted(keys)
        },
        "training_matches": len(training),
        "priors": PRIOR_SD,
    }
    if prior_rows is not None:
        artifact["history"] = {
            "pace_prior_sd": PACE_PRIOR_SD,
            "contexts": {
                key: {
                    "pace_key": value["pace_key"],
                    "matches": value["history_matches"],
                    "team_spread": value["spread"],
                }
                for key, value in sorted(priors.items())
            },
        }
    return artifact
