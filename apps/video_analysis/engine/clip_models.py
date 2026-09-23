"""Lightweight clip eligibility and allowlisted public failure messages."""

REQUIRED_CLASSES = frozenset({"ball", "player", "basket", "referee"})
MODEL_ERROR = (
    "This model cannot analyze full clips. Choose a model trained to detect "
    "ball, player, basket and referee. Older people-only models are not supported."
)
FAILURES = {"incompatible_model": MODEL_ERROR}


def supports_clips(classes: object) -> bool:
    """Use the frozen class taxonomy, never annotation counts or model imports."""
    return (
        isinstance(classes, list)
        and all(isinstance(label, str) for label in classes)
        and set(classes) >= REQUIRED_CLASSES
    )


def failure_message(record: dict, fallback: str) -> str:
    """Do not expose arbitrary subprocess exceptions or private paths."""
    code = record.get("failure_code")
    return FAILURES.get(code, fallback) if isinstance(code, str) else fallback
