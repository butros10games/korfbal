"""Capabilities and safe failures for private KNKV match forms."""

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class MatchFormOptions:
    """Optional publication identity and automatic-discovery behavior."""

    captain_player_id: UUID | None = None
    automatic: bool = False


class MatchFormError(Exception):
    """A bounded error code safe to return without provider payloads."""

    def __init__(self, code: str) -> None:
        """Retain only an application-defined error code."""
        self.code = code
        super().__init__(code)


class MatchFormProvider(Protocol):
    """Read forms and conditionally replace a freshly observed form."""

    def read(
        self, resource: str, match_id: str, *, home: bool | None = None
    ) -> dict[str, Any]:
        """Read one authorized form."""

    def find_player(self, match_id: str, home: bool, person_id: str) -> dict[str, Any]:
        """Resolve an exact eligible identity through the match's player search."""

    def replace(
        self,
        resource: str,
        match_id: str,
        original: dict[str, Any],
        updated: dict[str, Any],
        *,
        home: bool | None = None,
    ) -> dict[str, Any]:
        """Reject an intervening provider edit before sending a replacement."""
