"""Rolling observed-time backtests and paired promotion gates for score forecasts."""

from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import math

import numpy as np
from scipy.special import gammaln, logsumexp

from apps.competition.domain.score_forecast import (
    context_key,
    poisson,
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


def metric_summary(draws: list[list[float]], weights: dict | None = None) -> dict:
    """Vectorize the same normalized Poisson mixture used by the serving contract."""
    pairs = [(poisson(home), poisson(away)) for home, away in draws]
    home = np.zeros((len(pairs), max(len(h) for h, _ in pairs)))
    away = np.zeros((len(pairs), max(len(a) for _, a in pairs)))
    for index, (h, a) in enumerate(pairs):
        home[index, : len(h)] = h
        away[index, : len(a)] = a
    joint = home.T @ away / len(pairs)
    h, a = np.indices(joint.shape)
    masks = {"home": h > a, "draw": h == a, "away": h < a}
    if weights:
        joint *= sum(masks[key] * weights[key] for key in masks)
        joint /= joint.sum()
    marginals = {"home": joint.sum(axis=1), "away": joint.sum(axis=0)}
    return {
        "expected_goals": {
            side: float(np.arange(len(values)) @ values)
            if weights
            else float(np.mean(np.asarray(draws)[:, index]))
            for index, (side, values) in enumerate(marginals.items())
        },
        "interval_80": {
            side: [int(np.searchsorted(np.cumsum(values), q)) for q in (0.1, 0.9)]
            for side, values in marginals.items()
        },
        "probabilities": {key: float(joint[mask].sum()) for key, mask in masks.items()},
    }


def metrics(
    draws: list[list[float]], row: dict, outcome_weights: dict[str, float] | None = None
) -> dict:
    """Measure joint score loss, outcome loss, coverage and goal error together."""
    result = metric_summary(draws, outcome_weights)
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
                "legacy_available": "legacy_prediction" in row,
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
        and all(record["legacy_available"] for record in records)
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


def forward_audit(rows: list[dict], artifact: dict, through: datetime) -> dict:
    """Evaluate the exact served artifact after its real availability boundary.

    The artifact remains frozen. Labels are reconstructed from revisions observed by
    ``through`` while comparison baselines use only results known at the artifact's
    training cutoff.

    Raises:
        ValueError: Artifact provenance or the requested audit window is invalid.

    """
    training_cutoff = timestamp(artifact["training_cutoff"])
    available_from = timestamp(artifact["available_from"])
    if (
        artifact.get("approved") is not True
        or artifact.get("validation", {}).get("passed") is not True
        or training_cutoff > available_from
        or available_from >= through
    ):
        raise ValueError("Artifact or forward-audit window is invalid")

    training_groups: dict[str, list[dict]] = defaultdict(list)
    for row in snapshot(rows, training_cutoff):
        training_groups[context_key(row)].append(row)

    records: list[dict] = []
    excluded: Counter[str] = Counter()
    candidates = [
        row
        for row in snapshot(rows, through)
        if available_from <= timestamp(row["starts_at"]) < through
    ]
    for row in candidates:
        if timestamp(row["duration_observed_at"]) > timestamp(row["starts_at"]):
            excluded["duration_unknown_at_kickoff"] += 1
            continue
        draws = rate_draws(artifact, row)
        if draws is None:
            excluded["unsupported_artifact_context"] += 1
            continue
        group = training_groups.get(context_key(row))
        if not group:
            excluded["missing_training_context"] += 1
            continue

        # Match the rolling gate's uncertainty-aware pooled context comparator,
        # but freeze it at the deployed artifact's training cutoff.
        shape = 1 + sum(r["home_score"] + r["away_score"] for r in group)
        rate = 0.1 + sum(2 * r["duration"] / 60 for r in group)
        rng = np.random.default_rng(2026)
        pace = rng.gamma(shape, 1 / rate, len(draws)) * row["duration"] / 60
        pooled = [[float(value), float(value)] for value in pace]
        records.append({
            "context": context_key(row),
            "pool": row["pool"],
            "candidate": metrics(draws, row),
            "context_baseline": metrics(pooled, row),
            "legacy": legacy_metrics(row),
        })

    models = ("candidate", "context_baseline", "legacy")
    overall = {name: aggregate([record[name] for record in records]) for name in models}
    pools, intervals = paired_intervals(records, models)
    return {
        "mode": "deployed-forward-audit",
        "artifact": {
            "version": artifact["version"],
            "input_sha256": artifact.get("input_sha256"),
            "training_cutoff": artifact["training_cutoff"],
            "available_from": artifact["available_from"],
        },
        "through": through.isoformat(),
        "eligible_matches": len(candidates),
        "evaluated_matches": len(records),
        "evaluated_pools": len(pools),
        "excluded": dict(excluded),
        "metrics": overall,
        "paired_difference_95": intervals,
        "by_context": {
            key: aggregate([
                record["candidate"] for record in records if record["context"] == key
            ])
            for key in sorted({record["context"] for record in records})
        },
        "limitations": (
            "Retrospective current metadata; labels use revisions observed by the "
            "audit cutoff; live forecasts are not evaluated"
        ),
    }


def head_to_head(
    rows: list[dict], incumbent: dict, candidate: dict, through: datetime
) -> dict:
    """Compare two frozen artifacts on the same untouched matches and poules.

    The comparison begins only after both artifacts existed, preventing the newer
    candidate from benefiting from matches that were already known when it was fit.

    Raises:
        ValueError: Either artifact or the shared forward window is invalid.

    """
    for artifact in (incumbent, candidate):
        if (
            artifact.get("approved") is not True
            or artifact.get("validation", {}).get("passed") is not True
            or timestamp(artifact["training_cutoff"])
            > timestamp(artifact["available_from"])
        ):
            raise ValueError(
                "Head-to-head artifacts must be approved and chronological"
            )
    available_from = max(
        timestamp(incumbent["available_from"]),
        timestamp(candidate["available_from"]),
    )
    if available_from >= through:
        raise ValueError("Head-to-head window must follow both availability times")

    eligible = [
        row
        for row in snapshot(rows, through)
        if available_from <= timestamp(row["starts_at"]) < through
    ]
    records: list[dict] = []
    excluded: Counter[str] = Counter()
    for row in eligible:
        if timestamp(row["duration_observed_at"]) > timestamp(row["starts_at"]):
            excluded["duration_unknown_at_kickoff"] += 1
            continue
        incumbent_draws = rate_draws(incumbent, row)
        candidate_draws = rate_draws(candidate, row)
        if incumbent_draws is None or candidate_draws is None:
            excluded["not_supported_by_both"] += 1
            continue
        records.append({
            "pool": row["pool"],
            "candidate": metrics(candidate_draws, row),
            "incumbent": metrics(incumbent_draws, row),
        })

    pools, intervals = paired_intervals(records, ("candidate", "incumbent"))
    overall = {
        model: aggregate([record[model] for record in records])
        for model in ("candidate", "incumbent")
    }
    enough = len(records) >= MIN_TEST_MATCHES and len(pools) >= MIN_TEST_POOLS
    coverage_ok = (
        MIN_COVERAGE <= overall["candidate"].get("coverage_80", 0) <= MAX_COVERAGE
    )
    improved = (
        enough
        and coverage_ok
        and all(
            interval[1] is not None and interval[1] < 0
            for interval in intervals.values()
        )
    )
    worse = enough and all(
        interval[0] is not None and interval[0] > 0 for interval in intervals.values()
    )
    verdict = "improved" if improved else "worse" if worse else "inconclusive"
    if not enough:
        verdict = "insufficient_evidence"
    return {
        "mode": "artifact-head-to-head",
        "available_from": available_from.isoformat(),
        "through": through.isoformat(),
        "eligible_matches": len(eligible),
        "evaluated_matches": len(records),
        "evaluated_pools": len(pools),
        "excluded": dict(excluded),
        "metrics": overall,
        "paired_difference_95": intervals,
        "verdict": verdict,
        "gate": (
            "100 shared untouched matches across 10 poules; candidate-minus-incumbent "
            "paired 95% upper differences < 0 for score log-loss and Brier score; "
            "candidate marginal interval coverage 70-95%"
        ),
        "limitations": (
            "Retrospective current metadata; only matches supported by both frozen "
            "artifacts are compared; live forecasts are not evaluated"
        ),
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
        home, away = poisson(total * share), poisson(total * (1 - share))
        cumulative = 0.0
        win, draw = 0.0, 0.0
        for goals, probability in enumerate(home):
            opponent = away[goals] if goals < len(away) else 0.0
            win += probability * cumulative
            draw += probability * opponent
            cumulative += opponent
        if win + draw / 2 < expected:
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
