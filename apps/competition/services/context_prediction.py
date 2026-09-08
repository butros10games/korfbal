"""Apply frozen, held-out-tested context coefficients without extra database reads."""

from datetime import datetime
from functools import cache
import json
import math
from pathlib import Path


@cache
def model() -> dict:
    """Load the shipped artifact once per process; never fit on an API request."""
    return json.loads(
        (Path(__file__).parents[1] / "data/context_prediction_v1.json").read_text()
    )


def context_prediction(
    *,
    context: str,
    ratings: tuple[float, float],
    as_of: datetime,
    season: str,
    rating_scale: float,
) -> dict | None:
    """Keep unseen seasons/contexts and pre-training matches on the seeded model."""
    artifact = model()
    home_rating, away_rating = ratings
    if (
        season != artifact["season"]
        or not math.isclose(rating_scale, artifact["rating_scale"])
        or as_of < datetime.fromisoformat(artifact["available_from"])
        or not all(math.isfinite(value) for value in (home_rating, away_rating))
    ):
        return None
    coefficients = artifact["contexts"].get(context)
    if coefficients is None:
        return None
    odds = (
        coefficients["home_bias"]
        + coefficients["rating_slope"]
        * (home_rating - away_rating)
        / artifact["rating_scale"]
    )
    if not math.isfinite(odds):
        return None
    logits = (odds / 2, coefficients["draw_logit"], -odds / 2)
    weights = [math.exp(value - max(logits)) for value in logits]
    if not all(weights):
        return None
    total = sum(weights)
    return {
        "model": artifact["version"],
        "home": weights[0] / total,
        "draw": weights[1] / total,
        "away": weights[2] / total,
    }
