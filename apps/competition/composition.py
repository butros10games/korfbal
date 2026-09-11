"""Wire the production competition provider at the application boundary."""

from pathlib import Path

from django.conf import settings

from apps.competition.adapters.outbound.match_forms import SportlinkMatchForms
from apps.competition.adapters.outbound.notifications import dispatch_schedule_change
from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.adapters.outbound.tokens import TokenStore
from apps.competition.services.match_form_worker import drain
from apps.competition.services.schedule_notifications import ScheduleChangeDispatcher
from apps.competition.services.traffic import TrafficGate
from apps.game_tracker.composition import change_publisher


def competition_client(
    token: str = "", session_file: Path | None = None, user_agent: str = ""
) -> SportlinkClient:
    """Build a session with optional automatic, persisted OAuth renewal."""
    store = TokenStore(session_file) if session_file else None
    return SportlinkClient(store.access_token if store else token, store, user_agent)


def schedule_change_dispatcher() -> ScheduleChangeDispatcher:
    """Resolve job runtime at the composition boundary."""
    return dispatch_schedule_change


def run_match_form_queue() -> str:
    """Open the current private session only after the global lease is claimed."""
    clients = []

    def provider(gate: TrafficGate) -> SportlinkMatchForms:
        client = competition_client(
            session_file=Path(settings.SPORTLINK_SYNC_SESSION_FILE)
        )
        clients.append(client)
        return SportlinkMatchForms(client, gate)

    try:
        return drain(provider, change_publisher)
    finally:
        for client in clients:
            client.close()
