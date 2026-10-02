"""Dependency-free replay of observed results at a frozen point in time."""

from datetime import datetime

from apps.competition.domain.score_forecast import timestamp


MAX_SCORE = 150


def snapshot(rows: list[dict], cutoff: datetime) -> list[dict]:
    """Replay the latest observed score revision strictly before the training cutoff."""
    selected = []
    for row in rows:
        result = latest_result(row, cutoff)
        if result is None:
            continue
        if (
            not row.get("duration_observed_at")
            or timestamp(row["duration_observed_at"]) >= cutoff
        ):
            continue
        selected.append(result)
    return selected


def result_snapshot(rows: list[dict], cutoff: datetime) -> list[dict]:
    """Replay earlier-season results, which may have been imported long afterwards.

    A completed earlier match counts from kickoff with its first observed score;
    later corrections still count only from their own observation.
    """
    selected = []
    for row in rows:
        if timestamp(row["starts_at"]) >= cutoff or not row["revisions"]:
            continue
        known = [r for r in row["revisions"] if timestamp(r["observed_at"]) < cutoff]
        first = min(
            row["revisions"],
            key=lambda r: (timestamp(r["observed_at"]), r["revision"]),
        )
        result = verified_result(row, known or [first])
        if result is not None:
            selected.append(result)
    return selected


def latest_result(row: dict, cutoff: datetime) -> dict | None:
    """Return the row with its last verified score observed before the cutoff."""
    if timestamp(row["starts_at"]) >= cutoff:
        return None
    revisions = [r for r in row["revisions"] if timestamp(r["observed_at"]) < cutoff]
    return verified_result(row, revisions) if revisions else None


def verified_result(row: dict, revisions: list[dict]) -> dict | None:
    """Accept the latest of the given revisions only if it is a complete score."""
    result = max(revisions, key=lambda r: (timestamp(r["observed_at"]), r["revision"]))
    if result["status"] != "FINAL" or result["automatic_result"]:
        return None
    scores = [result["home_score"], result["away_score"]]
    if any(type(score) is not int or not 0 <= score <= MAX_SCORE for score in scores):
        return None
    # The feed has no verified-zero marker. Quarantine 0-0 rather than
    # silently treating scheduled placeholders as completed matches.
    if scores == [0, 0]:
        return None
    return {**row, "home_score": scores[0], "away_score": scores[1]}
