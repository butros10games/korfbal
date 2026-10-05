"""Stable public archive namespace and historical playing-season names."""

ARCHIVE_PREFIX = "archive:"


def season_names(edition: int) -> tuple[str, str, str]:
    """Name the autumn outdoor, indoor and spring outdoor seasons of an edition."""
    return (
        f"Voor seizoen {edition}",
        f"Zaal seizoen {edition}-{edition + 1}",
        f"Na seizoen {edition + 1}",
    )


def full_year_name(edition: int) -> str:
    """Name the outdoor season of poules that play both halves of an edition."""
    return f"Veld seizoen {edition}-{edition + 1}"
