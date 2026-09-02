"""Wire-level contracts shared by tournament realtime adapters."""


def tournament_group_name(tournament_id: str) -> str:
    """Return the stable Channels group for one tournament."""
    return f"korfbal.tournament.{tournament_id}"
