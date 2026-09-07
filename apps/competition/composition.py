"""Wire the production competition provider at the application boundary."""

from pathlib import Path

from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.adapters.outbound.tokens import TokenStore


def competition_client(
    token: str = "", session_file: Path | None = None, user_agent: str = ""
) -> SportlinkClient:
    """Build a session with optional automatic, persisted OAuth renewal."""
    store = TokenStore(session_file) if session_file else None
    return SportlinkClient(store.access_token if store else token, store, user_agent)
