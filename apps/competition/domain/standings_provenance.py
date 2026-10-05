"""Authority, finality and coverage of one poule's standings table.

Official provider standings live in ``PoolEntry.standing``. A table generated
from scored finals lives in ``PoolEntry.computed_standing`` (or, until the
staged conversion has run, in ``standing`` with the legacy ``Computed`` marker).
One authority decides a whole table, so official and generated rows are never
mixed. A synchronized, unfiltered or closed-season table is not proof of a final
ranking: only recorded final evidence makes an official table final, and a
generated table is always provisional, with unknown deductions.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any


OFFICIAL, COMPUTED, NONE, UNKNOWN = "official", "computed", "none", "unknown"
FINAL, PROVISIONAL = "final", "provisional"
PARTIAL, COMPLETE = "partial", "complete"
LEGACY_MARKER = "Computed"
CALCULATION = "computed-v2"
FINAL_REVIEW = "official-final-review-v1"
MAX_REVIEW_REFERENCE = 500
# Why a generated table exists: a poule with only public-site results, a closed
# poule whose official table stayed empty, or a converted legacy table.
REASONS = ("archive_results", "closed_blank_fallback", "legacy_conversion")
MAX_SAFE_INTEGER = 9007199254740991
VALUE_FIELDS = {
    "position": "Position",
    "played": "TotalMatches",
    "won": "Won",
    "drawn": "Draw",
    "lost": "Lost",
    "points": "TotalPoints",
    "penalty_points": "PenaltyPoints",
    "goals_for": "GoalsFor",
    "goals_against": "GoalsAgainst",
}


def is_legacy_computed(standing: object) -> bool:
    """Return whether a stored standing is a legacy generated row."""
    return isinstance(standing, Mapping) and standing.get(LEGACY_MARKER) is True


def is_official_standing(standing: object) -> bool:
    """Return whether a stored standing carries official provider values."""
    return (
        isinstance(standing, Mapping)
        and bool(standing)
        and not is_legacy_computed(standing)
    )


def generated_standing(standing: object, computed: object) -> dict[str, Any] | None:
    """Return a row's generated values; the new column wins over a legacy copy."""
    if isinstance(computed, Mapping):
        return dict(computed)
    if is_legacy_computed(standing) and isinstance(standing, Mapping):
        return {key: value for key, value in standing.items() if key != LEGACY_MARKER}
    return None


def table_source(*, official: bool, generated: bool, results_filtered: bool) -> str:
    """Decide which table a poule shows, never mixing official and generated rows.

    A filtered official feed publishes no reliable table. A separately
    generated table retains its own provisional authority and coverage.
    """
    if official:
        return NONE if results_filtered else OFFICIAL
    return COMPUTED if generated else NONE


def table_status(source: str, provenance: object) -> str:
    """Return final only for an official table with recorded final evidence."""
    if source == COMPUTED:
        return PROVISIONAL
    if source != OFFICIAL:
        return UNKNOWN
    official = _section(provenance, OFFICIAL)
    evidence = official.get("evidence")
    return (
        FINAL
        if official.get("status") == FINAL
        and isinstance(evidence, Mapping)
        and evidence.get("kind") == FINAL_REVIEW
        and isinstance(evidence.get("reference"), str)
        and 0 < len(evidence["reference"].strip()) <= MAX_REVIEW_REFERENCE
        and _digest(evidence.get("table_digest")) is not None
        and isinstance(provenance, Mapping)
        and evidence["table_digest"] == provenance.get("official_digest")
        else UNKNOWN
    )


def content_digest(rows: Iterable[tuple[int, Mapping[str, Any] | None]]) -> str:
    """Hash one complete table's membership and values, independent of polling."""
    payload = sorted((team_id, dict(values or {})) for team_id, values in rows)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]


def table_digest(
    source: str, provenance: object, fallback: str | None = None
) -> str | None:
    """Namespace a current authority's content key; timestamps do not affect it."""
    if source not in {OFFICIAL, COMPUTED}:
        return None
    raw = (
        provenance.get("official_digest")
        if source == OFFICIAL and isinstance(provenance, Mapping)
        else _section(provenance, COMPUTED).get("digest")
    )
    digest = _digest(raw) or _digest(fallback)
    return f"{source}:v1:{digest}" if digest is not None else None


def _digest(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value)
        else None
    )


def fixture_coverage(source: str, provenance: object) -> str:
    """Return recorded fixture coverage; a generated table is never complete."""
    if source not in {OFFICIAL, COMPUTED}:
        return UNKNOWN
    coverage = _section(provenance, source).get("coverage")
    allowed = {PARTIAL} if source == COMPUTED else {PARTIAL, COMPLETE}
    return coverage if coverage in allowed else UNKNOWN


def safe_integer(raw: object) -> int | None:
    """Parse an exact, JSON-safe integer; booleans and fractions stay unknown."""
    if isinstance(raw, bool):
        return None
    try:
        number = Decimal(str(raw))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if (
        not number.is_finite()
        or abs(number) > MAX_SAFE_INTEGER
        or number != number.to_integral_value()
    ):
        return None
    return int(number)


def table_values(values: Mapping[str, Any] | None) -> dict[str, int | None]:
    """Return compact values; penalties are already included in the points."""
    values = values or {}
    return {name: safe_integer(values.get(key)) for name, key in VALUE_FIELDS.items()}


def position(values: Mapping[str, Any] | None) -> int | None:
    """Return a usable table position (1 or higher)."""
    number = safe_integer((values or {}).get("Position"))
    return number if number is not None and number >= 1 else None


def tied(
    source: str, own: Mapping[str, Any], table: Iterable[Mapping[str, Any]]
) -> bool:
    """Return whether another row shares this row's place.

    Official tables decide ties themselves, so only a shared position counts.
    A generated table orders equal points approximately, so equal points count.
    """
    key = "Position" if source == OFFICIAL else "TotalPoints"
    value = safe_integer(own.get(key))
    if value is None:
        return False
    return Counter(safe_integer(row.get(key)) for row in table)[value] > 1


def computed_provenance(
    *, reason: str, results: int, partial: bool, digest: str, computed_at: str
) -> dict[str, Any]:
    """Describe a generated table; its coverage and deductions stay unproven.

    Raises:
        ValueError: The reason is not a known generated-table origin.

    """
    if reason not in REASONS:
        raise ValueError(f"Unknown generated-table reason: {reason}")
    return {
        "calculation": CALCULATION,
        "reason": reason,
        "status": PROVISIONAL,
        "coverage": PARTIAL if partial else UNKNOWN,
        "deductions": UNKNOWN,
        "results": results,
        "digest": digest,
        "computed_at": computed_at,
    }


def _section(provenance: object, name: str) -> Mapping[str, Any]:
    section = provenance.get(name) if isinstance(provenance, Mapping) else None
    return section if isinstance(section, Mapping) else {}
