"""Fit tuned match-impact weights from historical match data.

This command pulls finished matches from Postgres and builds per-team feature
vectors that are sufficient to compute team impact totals as a linear function
of a small set of weights.

It then runs a lightweight random search to find weights that better correlate
with real match outcomes (goal differential).

The goal is to propose a new algorithm version (e.g. v6) while keeping older
versions reproducible.
"""

from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import asdict, dataclass
import json
import math
import random
from statistics import mean
from typing import cast

from django.core.management.base import BaseCommand

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    MatchTeamImpactFeatures,
    ShotImpactWeights,
    compute_match_team_impact_features,
    shot_impact_weights_for_version,
)


@dataclass(frozen=True)
class CandidateWeights:
    """Candidate parameter set for a proposed algorithm version (e.g. v6)."""

    miss_for_penalty: float
    shot_against_total: float
    goal_against_total: float
    miss_against_total: float
    doorloop_concede_factor: float


SIGN_EPS = 1e-9


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or not xs:
        return 0.0
    mx = mean(xs)
    my = mean(ys)
    num = 0.0
    dx = 0.0
    dy = 0.0
    for x, y in zip(xs, ys, strict=True):
        a = x - mx
        b = y - my
        num += a * b
        dx += a * a
        dy += b * b
    denom = math.sqrt(dx * dy)
    if denom <= 0:
        return 0.0
    return num / denom


def _sign_accuracy(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or not xs:
        return 0.0
    correct = 0
    total = 0
    for x, y in zip(xs, ys, strict=True):
        if abs(y) < SIGN_EPS:
            continue
        total += 1
        if (x >= 0 and y > 0) or (x <= 0 and y < 0):
            correct += 1
    return (correct / total) if total else 0.0


def _impact_from_features(
    *,
    features: MatchTeamImpactFeatures,
    weights: ShotImpactWeights,
    doorloop_concede_factor: float,
) -> float:
    # goals_scored_points is independent of the tuned weights.
    total = features.goals_scored_points

    # Shooter miss penalties.
    total += -weights.miss_for_penalty * features.shooter_misses_weighted

    # Defensive shares.
    total += weights.shot_against_total * float(features.defended_shots)
    total += weights.goal_against_total * float(features.defended_goals)
    total += weights.miss_against_total * float(features.defended_misses)

    # Doorloop concede penalty.
    total += -doorloop_concede_factor * features.doorloop_concede_points_times_defenders

    return float(total)


def _sample_candidate(
    rng: random.Random,
    *,
    base: CandidateWeights,
) -> CandidateWeights:
    # Keep ranges conservative around current v5; these can be widened later.
    def jitter(value: float, scale: float) -> float:
        return value + rng.uniform(-scale, scale)

    miss_for = min(max(jitter(base.miss_for_penalty, 0.25), 0.2), 1.2)
    shot_against = min(max(jitter(base.shot_against_total, 0.25), -2.5), 0.0)
    goal_against = min(max(jitter(base.goal_against_total, 1.5), -12.0), -1.0)
    miss_against = min(max(jitter(base.miss_against_total, 0.4), 0.0), 2.0)
    doorloop_factor = min(max(jitter(base.doorloop_concede_factor, 0.04), 0.0), 0.25)

    return CandidateWeights(
        miss_for_penalty=miss_for,
        shot_against_total=shot_against,
        goal_against_total=goal_against,
        miss_against_total=miss_against,
        doorloop_concede_factor=doorloop_factor,
    )


def _load_match_rows(*, max_matches: int) -> list[dict[str, object]]:
    qs = (
        MatchData.objects
        .select_related(
            "match_link__home_team",
            "match_link__away_team",
        )
        .filter(status="finished")
        .order_by("id_uuid")
    )
    if max_matches > 0:
        qs = qs[:max_matches]

    match_rows: list[dict[str, object]] = []

    for md in qs.iterator(chunk_size=200):
        match = md.match_link
        if not match:
            continue

        home_team_id = str(match.home_team_id)
        away_team_id = str(match.away_team_id)
        features_by_team = compute_match_team_impact_features(
            match_data=md,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
        if home_team_id not in features_by_team:
            continue
        if away_team_id not in features_by_team:
            continue

        goal_diff = float(md.home_score - md.away_score)

        match_rows.append({
            "match_data_id": str(md.id_uuid),
            "goal_diff": goal_diff,
            "features_home": features_by_team[home_team_id],
            "features_away": features_by_team[away_team_id],
        })

    return match_rows


def _eval_candidate(
    dataset: list[dict[str, object]],
    cand: CandidateWeights,
) -> dict[str, float]:
    weights = ShotImpactWeights(
        miss_for_penalty=cand.miss_for_penalty,
        shot_against_total=cand.shot_against_total,
        goal_against_total=cand.goal_against_total,
        miss_against_total=cand.miss_against_total,
    )

    diffs: list[float] = []
    goals: list[float] = []

    for row in dataset:
        fh = cast(MatchTeamImpactFeatures, row["features_home"])
        fa = cast(MatchTeamImpactFeatures, row["features_away"])

        impact_home = _impact_from_features(
            features=fh,
            weights=weights,
            doorloop_concede_factor=cand.doorloop_concede_factor,
        )
        impact_away = _impact_from_features(
            features=fa,
            weights=weights,
            doorloop_concede_factor=cand.doorloop_concede_factor,
        )

        diffs.append(float(impact_home - impact_away))
        goals.append(float(cast(float, row["goal_diff"])))

    return {
        "pearson": _pearson(diffs, goals),
        "sign_acc": _sign_accuracy(diffs, goals),
    }


def _kfold_splits(
    *,
    n: int,
    k: int,
) -> list[tuple[list[int], list[int]]]:
    if n <= 0:
        return []
    k = max(2, min(k, n))

    indices = list(range(n))
    folds: list[list[int]] = [[] for _ in range(k)]
    for i, idx in enumerate(indices):
        folds[i % k].append(idx)

    splits: list[tuple[list[int], list[int]]] = []
    for fold_idx in range(k):
        valid_idx = folds[fold_idx]
        train_idx: list[int] = []
        for j in range(k):
            if j == fold_idx:
                continue
            train_idx.extend(folds[j])
        splits.append((train_idx, valid_idx))
    return splits


def _eval_candidate_kfold(
    *,
    rows: list[dict[str, object]],
    cand: CandidateWeights,
    k: int,
) -> dict[str, float]:
    splits = _kfold_splits(n=len(rows), k=k)
    if not splits:
        return {"pearson": 0.0, "sign_acc": 0.0}

    pearsons: list[float] = []
    signs: list[float] = []
    for _train_idx, valid_idx in splits:
        valid = [rows[i] for i in valid_idx]
        m = _eval_candidate(valid, cand)
        pearsons.append(float(m["pearson"]))
        signs.append(float(m["sign_acc"]))

    return {
        "pearson": float(mean(pearsons)),
        "sign_acc": float(mean(signs)),
    }


class Command(BaseCommand):
    """Management command entrypoint."""

    help = "Fit tuned match-impact weights (propose v6) from finished matches."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Add CLI arguments."""
        parser.add_argument(
            "--max-matches",
            type=int,
            default=0,
            help="Limit number of finished matches used (0 = all).",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=42,
            help="Random seed.",
        )
        parser.add_argument(
            "--iterations",
            type=int,
            default=1500,
            help="Number of random candidates to evaluate.",
        )
        parser.add_argument(
            "--train-frac",
            type=float,
            default=0.8,
            help="Train fraction for holdout validation (0..1).",
        )
        parser.add_argument(
            "--kfold",
            type=int,
            default=5,
            help="K for K-fold CV (used for selecting best candidate).",
        )
        parser.add_argument(
            "--output-json",
            type=str,
            default="",
            help="Optional path to write a JSON report.",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Run the fit and print a JSON report."""
        max_matches = cast(int, options.get("max_matches", 0))
        seed = cast(int, options.get("seed", 42))
        iterations = cast(int, options.get("iterations", 1500))
        train_frac = cast(float, options.get("train_frac", 0.8))
        kfold = cast(int, options.get("kfold", 5))
        output_json = str(options.get("output_json", "") or "").strip()

        # Deterministic random search (not for crypto).
        rng = random.Random(seed)  # noqa: S311

        self.stdout.write("Loading finished matches and building features...")
        match_rows = _load_match_rows(max_matches=max_matches)

        if not match_rows:
            self.stdout.write(self.style.WARNING("No usable finished matches found."))
            return

        rng.shuffle(match_rows)
        split = int(len(match_rows) * max(0.0, min(train_frac, 1.0)))
        train = match_rows[:split] or match_rows
        valid = match_rows[split:] or match_rows

        # Baseline = current v5 shot weights + current doorloop concede factor.
        v5 = shot_impact_weights_for_version("v5")
        base = CandidateWeights(
            miss_for_penalty=v5.miss_for_penalty,
            shot_against_total=v5.shot_against_total,
            goal_against_total=v5.goal_against_total,
            miss_against_total=v5.miss_against_total,
            doorloop_concede_factor=0.06,
        )

        baseline_train = _eval_candidate(train, base)
        baseline_valid = _eval_candidate(valid, base)
        baseline_cv = _eval_candidate_kfold(rows=match_rows, cand=base, k=kfold)

        best = base
        best_score = float(baseline_cv["pearson"])

        for _ in range(iterations):
            cand = _sample_candidate(rng, base=best)
            metrics_cv = _eval_candidate_kfold(rows=match_rows, cand=cand, k=kfold)
            score = float(metrics_cv["pearson"])
            if score > best_score:
                best = cand
                best_score = score

        best_train = _eval_candidate(train, best)
        best_valid = _eval_candidate(valid, best)
        best_cv = _eval_candidate_kfold(rows=match_rows, cand=best, k=kfold)

        report = {
            "matches": {
                "total": len(match_rows),
                "train": len(train),
                "valid": len(valid),
            },
            "baseline_v5": {
                "candidate": asdict(base),
                "train": baseline_train,
                "valid": baseline_valid,
                "kfold": baseline_cv,
            },
            "best_candidate": {
                "candidate": asdict(best),
                "train": best_train,
                "valid": best_valid,
                "kfold": best_cv,
            },
        }

        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))

        if output_json:
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, sort_keys=True)
            self.stdout.write(self.style.SUCCESS(f"Wrote report to {output_json}"))
