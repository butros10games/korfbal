"""Wire the production competition provider at the application boundary."""

from pathlib import Path

from apps.competition.adapters.outbound.notifications import dispatch_schedule_change
from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.adapters.outbound.tokens import TokenStore
from apps.competition.services.schedule_notifications import ScheduleChangeDispatcher


def competition_client(
    token: str = "", session_file: Path | None = None, user_agent: str = ""
) -> SportlinkClient:
    """Build a session with optional automatic, persisted OAuth renewal."""
    store = TokenStore(session_file) if session_file else None
    return SportlinkClient(store.access_token if store else token, store, user_agent)


def schedule_change_dispatcher() -> ScheduleChangeDispatcher:
    """Resolve job runtime at the composition boundary."""
    return dispatch_schedule_change
