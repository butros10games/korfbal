"""Wire contract shared by match-change publishers and SSE consumers."""

from __future__ import annotations

from enum import StrEnum


class LiveResource(StrEnum):
    """React Query resource families affected by a match mutation."""

    LIVE = "live"
    TRACKER = "tracker"
    SUMMARY = "summary"
    EVENTS = "events"
    SHOTS = "shots"
    STATS = "stats"
    IMPACTS = "impacts"
    PLAYER_GROUPS = "player_groups"
    MVP = "mvp"


ALL_LIVE_RESOURCES = frozenset(LiveResource)
