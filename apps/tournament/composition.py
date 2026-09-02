"""Production composition root for tournament use cases."""

from functools import partial

from apps.tournament.adapters.outbound.realtime import (
    ChannelsTournamentChangePublisher,
)
from apps.tournament.services.live import touch_tournament as _touch_tournament


change_publisher = ChannelsTournamentChangePublisher()
touch_tournament = partial(_touch_tournament, publisher=change_publisher)
