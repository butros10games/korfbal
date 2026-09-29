"""Clock labels shown for a match before live tracking provides a running timer."""

SECONDS_PER_MINUTE = 60


def format_part_length(seconds: float) -> str:
    """Format a period length as zero-padded ``MM:SS``.

    Returns:
        The period length label, for example ``"25:00"``.

    """
    minutes = int(seconds / SECONDS_PER_MINUTE)
    remainder = int(seconds % SECONDS_PER_MINUTE)
    return f"{minutes:02d}:{remainder:02d}"
