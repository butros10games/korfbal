"""Guard the scope, training cutoff and request cost of context predictions."""

from datetime import datetime, timedelta
from typing import TypedDict

import pytest

from apps.competition.services.context_prediction import context_prediction, model


CONTEXT = "outdoor|b|youth_colour|youth|green|four"


class PredictionOptions(TypedDict):
    """Typed request-boundary inputs for synthetic predictions."""

    context: str
    ratings: tuple[float, float]
    season: str
    as_of: datetime
    rating_scale: float


def options() -> PredictionOptions:
    """Use a covered future match without needing database state."""
    artifact = model()
    return {
        "context": CONTEXT,
        "ratings": (50.0, 50.0),
        "season": artifact["season"],
        "as_of": datetime.fromisoformat(artifact["available_from"]),
        "rating_scale": 20,
    }


def test_context_prediction_is_normalized_and_has_no_database_dependency() -> None:
    """The regular pytest database blocker protects this pure serving path."""
    result = context_prediction(**options())
    assert result is not None
    assert result["model"] == "knkv-context-v1"
    assert sum(result[key] for key in ("home", "draw", "away")) == pytest.approx(1)
    assert all(0 < result[key] < 1 for key in ("home", "draw", "away"))
    stronger_options = options()
    stronger_options["ratings"] = (60.0, 50.0)
    stronger = context_prediction(**stronger_options)
    assert stronger is not None
    assert stronger["home"] > result["home"]


@pytest.mark.parametrize(
    "case", ["indoor", "a_category", "season", "scale", "nonfinite"]
)
def test_unsupported_inputs_keep_the_existing_model(case: str) -> None:
    """Never extrapolate learned baseline units to other classes or seasons."""
    parameters = options()
    if case == "indoor":
        parameters["context"] = "indoor|b|youth_colour|youth|green|four"
    elif case == "a_category":
        parameters["context"] = "outdoor|a|class_1|senior|unknown|eight"
    elif case == "season":
        parameters["season"] = "2027-2028"
    elif case == "scale":
        parameters["rating_scale"] = 400
    else:
        parameters["ratings"] = (float("nan"), 50)
    assert context_prediction(**parameters) is None


def test_training_labels_cannot_leak_into_historical_match_predictions() -> None:
    """Even a covered class must use the old prior before training was available."""
    parameters = options()
    parameters["as_of"] -= timedelta(microseconds=1)
    assert context_prediction(**parameters) is None
