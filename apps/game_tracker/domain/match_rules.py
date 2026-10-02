"""The rule profile one tracked match is played under.

Tracker writes, state reads, clocks, minutes and eligibility read the same
snapshot. An empty snapshot is the legacy assumption every match used before
profiles existed (two 30-minute halves, eight substitutions, two time-outs):
it stays usable, but is marked as assumed so derived data can tell it apart
from an officially resolved rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


SNAPSHOT_VERSION = "match-rules-v1"

OFFICIAL_TIMING = "official_timing"
EDITION_RULES = "edition_rules"
MANUAL = "manual"
ASSUMED_DEFAULT = "assumed_default"
SOURCES = (OFFICIAL_TIMING, EDITION_RULES, MANUAL, ASSUMED_DEFAULT)

LIMITED = "limited"
UNLIMITED = "unlimited"
NOT_APPLICABLE = "not_applicable"
UNKNOWN = "unknown"

LEGACY_PERIODS = (30, 30)
LEGACY_SUBSTITUTIONS = 8
LEGACY_TIMEOUTS = 2


@dataclass(frozen=True)
class AdditionalPeriod:
    """Extra time with its own length; never folded into regulation periods."""

    description: str
    minutes: int

    def as_payload(self) -> dict[str, object]:
        """Serialize the period."""
        return {"description": self.description, "minutes": self.minutes}


@dataclass(frozen=True)
class MatchRules:
    """Resolved rules; ``None`` values are explicitly unresolved."""

    source: str = ASSUMED_DEFAULT
    rule_version: str | None = None
    periods: tuple[int, ...] | None = LEGACY_PERIODS
    additional_periods: tuple[AdditionalPeriod, ...] = ()
    players_per_team: int | None = None
    clock: str | None = None
    shot_clock: bool | None = None
    substitutions: str = UNKNOWN
    substitution_limit: int | None = None
    timeouts: str = UNKNOWN
    timeout_limit: int | None = None
    unresolved: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def assumed(self) -> bool:
        """Tell whether the timing is the legacy assumption, not a resolved rule."""
        return self.source == ASSUMED_DEFAULT

    @property
    def duration_resolved(self) -> bool:
        """Tell whether the regulation duration is known rather than assumed."""
        return self.periods is not None and not self.assumed

    @property
    def regulation_minutes(self) -> int | None:
        """Return the regulation duration, without extra time."""
        return sum(self.periods) if self.periods else None

    @property
    def uniform(self) -> bool:
        """Tell whether the tracker clock can run the regulation periods."""
        return bool(self.periods) and len(set(self.periods or ())) == 1

    def tracker_clock(self) -> tuple[int, int] | None:
        """Return ``(parts, part_length_seconds)`` for the tracker, if representable."""
        if not self.periods or not self.uniform:
            return None
        return len(self.periods), self.periods[0] * 60

    def effective_substitution_limit(self) -> int | None:
        """Return the enforced limit; ``None`` allows unlimited substitutions.

        An unresolved rule keeps the legacy limit rather than lifting it.
        """
        if self.substitutions == UNLIMITED:
            return None
        if self.substitutions == LIMITED and self.substitution_limit is not None:
            return self.substitution_limit
        return LEGACY_SUBSTITUTIONS

    def effective_timeout_limit(self) -> int:
        """Return the enforced time-out limit; 0 when time-outs do not apply."""
        if self.timeouts == NOT_APPLICABLE:
            return 0
        if self.timeouts == LIMITED and self.timeout_limit is not None:
            return self.timeout_limit
        return LEGACY_TIMEOUTS

    def as_snapshot(self) -> dict[str, Any]:
        """Serialize for storage on the match."""
        return {
            "version": SNAPSHOT_VERSION,
            "source": self.source,
            "rule_version": self.rule_version,
            "periods": list(self.periods) if self.periods is not None else None,
            "additional_periods": [
                period.as_payload() for period in self.additional_periods
            ],
            "players_per_team": self.players_per_team,
            "clock": self.clock,
            "shot_clock": self.shot_clock,
            "substitutions": self.substitutions,
            "substitution_limit": self.substitution_limit,
            "timeouts": self.timeouts,
            "timeout_limit": self.timeout_limit,
            "unresolved": list(self.unresolved),
            "evidence": dict(self.evidence),
        }

    def as_payload(self) -> dict[str, Any]:
        """Serialize for clients, with the limits commands actually enforce."""
        return {
            **self.as_snapshot(),
            "regulation_minutes": self.regulation_minutes,
            "assumed": self.assumed,
            "duration_resolved": self.duration_resolved,
            "enforced": {
                "substitutions": self.effective_substitution_limit(),
                "timeouts": self.effective_timeout_limit(),
            },
        }


def legacy_rules(parts: int, part_length: int) -> MatchRules:
    """Describe an existing match configured before rule profiles existed."""
    minutes, remainder = divmod(part_length, 60)
    periods = tuple([minutes] * parts) if parts and not remainder else None
    return MatchRules(
        periods=periods,
        unresolved=(
            "periods",
            "players_per_team",
            "clock",
            "shot_clock",
            "substitutions",
            "timeouts",
        ),
    )


def rules_from_snapshot(
    snapshot: Mapping[str, Any] | None, *, parts: int = 2, part_length: int = 1800
) -> MatchRules:
    """Read a stored snapshot; an empty one is the legacy assumption."""
    if not snapshot or snapshot.get("version") != SNAPSHOT_VERSION:
        return legacy_rules(parts, part_length)
    periods = snapshot.get("periods")
    return MatchRules(
        source=str(snapshot.get("source") or ASSUMED_DEFAULT),
        rule_version=snapshot.get("rule_version"),
        periods=tuple(int(value) for value in periods) if periods else None,
        additional_periods=tuple(
            AdditionalPeriod(str(row["description"]), int(row["minutes"]))
            for row in snapshot.get("additional_periods") or ()
        ),
        players_per_team=snapshot.get("players_per_team"),
        clock=snapshot.get("clock"),
        shot_clock=snapshot.get("shot_clock"),
        substitutions=str(snapshot.get("substitutions") or UNKNOWN),
        substitution_limit=snapshot.get("substitution_limit"),
        timeouts=str(snapshot.get("timeouts") or UNKNOWN),
        timeout_limit=snapshot.get("timeout_limit"),
        unresolved=tuple(snapshot.get("unresolved") or ()),
        evidence=dict(snapshot.get("evidence") or {}),
    )
