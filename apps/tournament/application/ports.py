"""Outbound capabilities used by tournament application services."""

from __future__ import annotations

from typing import Protocol


class TournamentChangePublisher(Protocol):
    """Publish a committed tournament revision to realtime consumers."""

    def publish(self, *, tournament_id: str, revision: int) -> None:
        """Publish one committed tournament change."""
