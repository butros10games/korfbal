"""Rolling observed-time backtests and paired promotion gates for score forecasts."""

from collections import defaultdict
from datetime import datetime
from hashlib import sha256
import math

import numpy as np
from scipy.special import gammaln, logsumexp

from apps.competition.domain.score_forecast import (
    context_key,
    rate_draws,
    summarize,
    timestamp,
)
from apps.competition.offline.score_training import fit, snapshot


MIN_TEST_MATCHES = 100
MIN_TEST_POOLS = 10
MIN_ORIGINS = 2
MIN_COVERAGE = 0.7
MAX_COVERAGE = 0.95

LEGACY_TOTALS = {"outdoor": 31.7, "indoor": 43.36363636363637}


def metrics(
    draws: list[list[float]], row: dict, outcome_weights: dict[str, float] | None = None
) -> dict:
    """Measure joint score loss, outcome loss, coverage and goal error together."""
    result = summarize(draws, outcome_weights)
    observed = np.array([row["home_score"], row["away_score"]])
    rates = np.asarray(draws)
    log_density = np.sum(
        observed * np.log(rates) - rates - gammaln(observed + 1), axis=1
    )
    outcome = (
        "home"
        if observed[0] > observed[1]
        else "away"
        if observed[0] < observed[1]
        else "draw"
    )
    probabilities = result["probabilities"]
    if outcome_weights:
        log_density += math.log(outcome_weights[outcome])
    return {
        "score_log_loss": float(math.log(len(draws)) - logsumexp(log_density)),
        "brier": sum(
            (p - int(key == outcome)) ** 2 for key, p in probabilities.items()
        ),
        "goal_mae": sum(
            abs(result["expected_goals"][side] - row[f"{side}_score"])
            for side in ("home", "away")
        )
        / 2,
        "coverage_80": sum(
            result["interval_80"][side][0]
            <= row[f"{side}_score"]
            <= result["interval_80"][side][1]
            for side in ("home", "away")
        )
        / 2,
        "probabilities": probabilities,
        "outcome": outcome,
    }


def aggregate(items: list[dict]) -> dict:
    """Keep calibration bins and group counts alongside overall mean metrics."""
    if not items:
        return {"matches": 0}
    bins = {}
    for outcome in ("home", "draw", "away"):
        bins[outcome] = []
        for index in range(10):
            selected = [
                item
                for item in items
                if min(9, int(item["probabilities"][outcome] * 10)) == index
            ]
            if selected:
                bins[outcome].append({
                    "n": len(selected),
                    "predicted": sum(
                        item["probabilities"][outcome] for item in selected
                    )
                    / len(selected),
                    "observed": sum(item["outcome"] == outcome for item in selected)
                    / len(selected),
                })
    return {
        "matches": len(items),
        **{
            key: sum(item[key] for item in items) / len(items)
            for key in ("score_log_loss", "brier", "goal_mae", "coverage_80")
        },
        "calibration": bins,
    }


def backtest(
    rows: list[dict],
    cutoffs: list[datetime],
    through: datetime,
    *,
    cold_start: bool = False,
) -> dict:
    """Fit at each origin using only already-observed labels."""
    records: list[dict] = []
    folds = []
    labels = snapshot(rows, through)
    for index, cutoff in enumerate(cutoffs):
        end = cutoffs[index + 1] if index + 1 < len(cutoffs) else through
        training_rows = rows
        if cold_start:
            training_rows = [
                row
                for row in rows
                if int(sha256(row["pool"].encode()).hexdigest(), 16) % 5 != 0
            ]
        artifact = fit(training_rows, cutoff)
        training = snapshot(training_rows, cutoff)
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in training:
            groups[context_key(row)].append(row)
        fold_count = 0
        for row in labels:
            if not cutoff <= timestamp(row["starts_at"]) < end:
                continue
            if timestamp(row["duration_observed_at"]) > timestamp(row["starts_at"]):
                continue
            if cold_start and any(
                row["pool"] == train["pool"] for train in training_rows
            ):
                continue
            draws = rate_draws(artifact, row)
            if draws is None:
                continue
            group = groups[context_key(row)]
            # Conjugate Gamma-Poisson pooled context baseline includes uncertainty;
            # identical format and exposure, without team or poule effects.
            shape = 1 + sum(r["home_score"] + r["away_score"] for r in group)
            rate = 0.1 + sum(2 * r["duration"] / 60 for r in group)
            rng = np.random.default_rng(2026)
            pace = rng.gamma(shape, 1 / rate, 64) * row["duration"] / 60
            pooled = [[float(value), float(value)] for value in pace]
            legacy = legacy_metrics(row)
            records.append({
                "context": context_key(row),
                "pool": row["pool"],
                "candidate": metrics(draws, row),
                "context_baseline": metrics(pooled, row),
                "legacy": legacy,
            })
            fold_count += 1
        folds.append({
            "cutoff": cutoff.isoformat(),
            "end": end.isoformat(),
            "training": len(training),
            "tested": fold_count,
        })
    models = ("candidate", "context_baseline", "legacy")
    overall = {name: aggregate([r[name] for r in records]) for name in models}
    pools, intervals = paired_intervals(records, models)
    passed = (
        not cold_start
        and all("legacy_prediction" in row for row in rows)
        and len(records) >= MIN_TEST_MATCHES
        and len(pools) >= MIN_TEST_POOLS
        and len(cutoffs) >= MIN_ORIGINS
        and all(fold["tested"] for fold in folds)
        and all(
            bounds[1] is not None and bounds[1] < 0 for bounds in intervals.values()
        )
        and MIN_COVERAGE <= overall["candidate"].get("coverage_80", 0) <= MAX_COVERAGE
    )
    return {
        "passed": passed,
        "mode": "unseen-poule" if cold_start else "rolling-origin",
        "folds": folds,
        "metrics": overall,
        "paired_difference_95": intervals,
        "by_context": {
            key: aggregate([r["candidate"] for r in records if r["context"] == key])
            for key in sorted({r["context"] for r in records})
        },
        "gate": (
            "100 matches, 10 poules, 2 nonempty origins; paired 95% upper "
            "differences < 0 versus both baselines; marginal interval coverage 70-95%"
        ),
        "limitations": ("Retrospective current metadata; live forecasts not evaluated"),
    }


def paired_intervals(
    records: list[dict], models: tuple[str, ...]
) -> tuple[list[str], dict]:
    """Quantify paired differences with a poule-cluster bootstrap."""
    # Poule-cluster bootstrap preserves within-poule dependence when quantifying
    # paired differences. Report uncertainty, not just a favourable point estimate.
    pools = sorted({r["pool"] for r in records})
    intervals = {}
    rng = np.random.default_rng(2026)
    for baseline in models[1:]:
        for metric in ("score_log_loss", "brier"):
            grouped = [
                [
                    r["candidate"][metric] - r[baseline][metric]
                    for r in records
                    if r["pool"] == pool
                ]
                for pool in pools
            ]
            means = []
            if grouped:
                totals = np.array([sum(group) for group in grouped])
                sizes = np.array([len(group) for group in grouped])
                for _ in range(1000):
                    sampled = rng.integers(len(pools), size=len(pools))
                    means.append(float(totals[sampled].sum() / sizes[sampled].sum()))
            intervals[f"{baseline}:{metric}"] = (
                np.quantile(means, [0.025, 0.975]).tolist() if means else [None, None]
            )
    return pools, intervals


def legacy_metrics(row: dict) -> dict:
    """Reproduce the old fixed-total score split and optional outcome reweighting."""
    prediction = row.get("legacy_prediction", {"status": "unavailable"})
    total = LEGACY_TOTALS[row["discipline"]]
    expected = prediction.get("home_expected_result", 0.5)
    low, high = 0.000001, 0.999999
    for _ in range(35):
        share = (low + high) / 2
        probabilities = summarize([[total * share, total * (1 - share)]])[
            "probabilities"
        ]
        if probabilities["home"] + probabilities["draw"] / 2 < expected:
            low = share
        else:
            high = share
    share = (low + high) / 2
    draws = [[total * share, total * (1 - share)]]
    calibrated = prediction.get("outcome_calibration")
    weights = None
    if calibrated:
        raw = summarize(draws)["probabilities"]
        weights = {key: calibrated[key] / raw[key] for key in ("home", "draw", "away")}
    return metrics(draws, row, weights)
