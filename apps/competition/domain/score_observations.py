"""Dependency-free replay of observed results at a frozen point in time."""

from datetime import datetime

from apps.competition.domain.score_forecast import timestamp


MAX_SCORE = 150


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
