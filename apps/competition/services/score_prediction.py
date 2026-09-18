"""Serve approved immutable score artifacts without fitting or history queries."""

from datetime import datetime
from functools import lru_cache
import json
import logging
from pathlib import Path

from django.conf import settings

from apps.competition.domain.score_forecast import (
    DRAW_COUNT,
    VERSION,
    context_key,
    rate_draws,
    summarize,
    timestamp,
)


logger = logging.getLogger(__name__)
DEVELOPING_MATCHES = 5


@lru_cache(maxsize=4)
def load_artifact(path: str) -> dict | None:
    """Load a versioned path once per worker; invalid artifacts fail closed."""
    if not path:
        return None
    try:
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
        if artifact["version"] != VERSION or artifact["approved"] is not True:
            return None
        if (
            artifact["draw_count"] != DRAW_COUNT
            or not artifact["contexts"]
            or timestamp(artifact["training_cutoff"])
            > timestamp(artifact["available_from"])
        ):
            return None
        if (
            not isinstance(artifact.get("validation"), dict)
            or artifact["validation"].get("passed") is not True
        ):
            return None
        return artifact
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Score forecast artifact unavailable or invalid")
        return None


def score_prediction(row: dict, as_of: datetime) -> dict | None:
    """Return a bounded posterior mixture and its internally consistent summaries."""
    artifact = load_artifact(getattr(settings, "KORFBAL_SCORE_FORECAST_ARTIFACT", ""))
    if artifact is None or as_of < timestamp(artifact["available_from"]):
        return None
    if (
        not row.get("duration_observed_at")
        or timestamp(row["duration_observed_at"]) > as_of
    ):
        return None
    try:
        draws = rate_draws(artifact, row)
        if draws is None:
            return None
        group = artifact["contexts"][context_key(row)]
        if len(draws) != DRAW_COUNT:
            return None
        samples = {
            "context": group["matches"],
            "pool": group["pool_matches"].get(row["pool"], 0),
            "home": group["team_matches"].get(row["home"], 0),
            "away": group["team_matches"].get(row["away"], 0),
        }
        return {
            "status": "scored",
            "model": artifact["version"],
            "as_of": as_of.isoformat(),
            "trained_through": artifact["training_cutoff"],
            "available_from": artifact["available_from"],
            "duration": row["duration"],
            "rate_draws": draws,
            "context": {
                key: row[key]
                for key in (
                    "discipline",
                    "phase",
                    "category",
                    "age_group",
                    "colour",
                    "playing_format",
                    "class_code",
                    "pool_label",
                )
            },
            "samples": samples,
            "evidence": "limited"
            if min(samples["home"], samples["away"], samples["pool"])
            < DEVELOPING_MATCHES
            else "developing",
            "live_validated": False,
            **summarize(draws),
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        logger.warning("Unsupported score forecast parameters")
        return None
