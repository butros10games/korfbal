"""Public provider-result values, separate from the tracker state machine."""

from typing import Literal, TypedDict


SourceStatus = Literal[
    "scheduled", "final", "suspended", "postponed", "cancelled", "unknown"
]
SourceAuthority = Literal["provider", "local"]
ResultSource = Literal["knkv", "archive"]
SOURCE_STATUSES: dict[str, SourceStatus] = {
    "SCHEDULED": "scheduled",
    "FINAL": "final",
    "SUSPENDED": "suspended",
    "POSTPONED": "postponed",
    "CANCELLED": "cancelled",
    "WITHDRAWN": "cancelled",
}


class SourceScore(TypedDict):
    """Each source side is independently known or unknown."""

    home: int | None
    away: int | None


class SourceResult(TypedDict):
    """Source observation and the owner allowed to control its display."""

    status: SourceStatus
    score: SourceScore
    is_final: bool
    source: ResultSource
    display_authority: SourceAuthority


def normalize_source_result(
    *,
    status: str,
    home_score: int | None,
    away_score: int | None,
    source: ResultSource,
    display_authority: SourceAuthority,
) -> SourceResult:
    """Normalize public state without inventing a score or a played final."""
    normalized = SOURCE_STATUSES.get(status, "unknown")
    return {
        "status": normalized,
        "score": {"home": home_score, "away": away_score},
        "is_final": (
            normalized == "final" and home_score is not None and away_score is not None
        ),
        "source": source,
        "display_authority": display_authority,
    }
