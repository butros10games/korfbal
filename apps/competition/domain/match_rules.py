"""Resolve a KNKV match rule profile from official timing and competition context.

Only rules verified for an edition are applied; earlier editions keep every
rule unresolved unless the provider reported the match's own periods.

2026-2027 sources (verified 2 October 2026):
- Competitiehandboek part 1, 7.5: durations per category, age and colour;
  four-player B competitions play 4 x 10 minutes.
  https://www.knkv.nl/kennisbank/competitiehandboek/
- Competitiehandboek part 2: 9.3 durations (2 x 25 minutes "zuivere speeltijd"
  with a shot clock, 2 x 30 without), 9.4 time-outs in topkorfbal and the
  A-category only, 9.6.1 eight substitutions in topkorfbal/A, 9.6.2 unlimited
  substitutions in the B-category, 9.10 extra time, and 9.2: the indoor
  classes that use a shot clock (no outdoor competition does).
  https://www.knkv.nl/kennisbank/competitiehandboek-2/
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.game_tracker.domain.match_rules import (
    EDITION_RULES,
    LIMITED,
    NOT_APPLICABLE,
    OFFICIAL_TIMING,
    UNKNOWN,
    UNLIMITED,
    AdditionalPeriod,
    MatchRules,
)


VERIFIED_EDITIONS = frozenset({2026})
A_SUBSTITUTIONS = 8
A_TIMEOUTS = 2
SHOT_CLOCK_PERIODS = (25, 25)
RUNNING_PERIODS = (30, 30)
# Provider period descriptions that are not regulation time.
ADDITIONAL = {"1e verlenging", "2e verlenging", "verlenging", "golden goal"}
SHOOTOUT = {"strafworpserie"}
B_FOUR_COLOURS = {"red", "orange", "yellow", "green", "blue"}


@dataclass(frozen=True)
class RuleContext:
    """Competition context rules are selected by; unknown fields stay None."""

    edition: int | None = None
    discipline: str | None = None
    category: str | None = None
    age_group: str | None = None
    colour: str | None = None
    playing_format: str | None = None
    # Official class code (hoofdklasse, class_1, ...), gender and team kind
    # (standard, reserve, youth) of the poule.
    code: str | None = None
    gender: str | None = None
    team_kind: str | None = None

    def known(self, value: str | None) -> str | None:
        """Normalize the classification's explicit unknown marker."""
        return None if value in {None, "", "unknown"} else value


def rule_version(edition: int | None) -> str | None:
    """Return the version label of an edition's verified rules."""
    return f"knkv-{edition}-{edition + 1}" if edition in VERIFIED_EDITIONS else None


# (category, playing format, colour, age group, discipline) -> periods, where
# None matches any value. The first matching row wins.
CLASS_PERIODS: tuple[tuple[tuple[str | None, ...], tuple[int, ...]], ...] = (
    *(
        ((("b", "four", colour, None, None)), (10, 10, 10, 10))
        for colour in B_FOUR_COLOURS
    ),
    (("b", "eight", "orange", None, None), (25, 25)),
    (("b", "eight", "yellow", None, None), (25, 25)),
    (("b", "eight", "red", None, None), RUNNING_PERIODS),
    (("b", "eight", "", "senior", None), RUNNING_PERIODS),
    (("a", None, None, "U15", "indoor"), (25, 25)),
    (("a", None, None, "U15", "outdoor"), (25, 25)),
    (("a", None, None, "senior", "outdoor"), RUNNING_PERIODS),
    (("a", None, None, "U19", "outdoor"), RUNNING_PERIODS),
    (("a", None, None, "U17", "outdoor"), RUNNING_PERIODS),
    # 7.5.1: U17 plays 2 x 25 indoors with and without a shot clock.
    (("a", None, None, "U17", "indoor"), SHOT_CLOCK_PERIODS),
)


# KNKV 9.2 (2026-2027): every indoor class that plays with a shot clock, per
# gender and age group. The list is exhaustive; other indoor A classes play
# without one.
SHOT_CLOCK_CLASSES: dict[tuple[str, str, str], frozenset[str]] = {
    # Senior classes per team kind: reserve teams have a shot clock only in the
    # reserve classes 9.2 names (no reserve 1e klasse, no women's reserve
    # hoofdklasse).
    ("mixed", "senior", "standard"): frozenset({
        "league",
        "league_2",
        "hoofdklasse",
        "overgangsklasse",
        "class_1",
    }),
    ("mixed", "senior", "reserve"): frozenset({
        "league",
        "league_2",
        "hoofdklasse",
        "overgangsklasse",
    }),
    ("women", "senior", "standard"): frozenset({"topklasse", "hoofdklasse"}),
    ("women", "senior", "reserve"): frozenset({"topklasse"}),
    ("mixed", "U19", "youth"): frozenset({"hoofdklasse", "overgangsklasse"}),
    ("mixed", "U17", "youth"): frozenset({"hoofdklasse"}),
    ("mixed", "U15", "youth"): frozenset(),
    ("women", "U19", "youth"): frozenset({"hoofdklasse"}),
    ("women", "U17", "youth"): frozenset(),
    ("women", "U15", "youth"): frozenset(),
}
SENIOR_KINDS = ("standard", "reserve")


def class_shot_clock(context: RuleContext) -> bool | None:
    """Return whether a verified class plays with a shot clock, if known.

    A senior class without a known team kind is decided only when standard and
    reserve teams agree on it.
    """
    if context.edition not in VERIFIED_EDITIONS:
        return None
    category = context.known(context.category)
    discipline = context.known(context.discipline)
    if category == "b" or (category in {"a", "top"} and discipline == "outdoor"):
        return False
    gender = context.known(context.gender) or ""
    age = context.known(context.age_group) or ""
    code = context.known(context.code)
    if category not in {"a", "top"} or discipline != "indoor" or code is None:
        return None
    kind = context.known(context.team_kind)
    kinds = (
        ("youth",)
        if age != "senior"
        else (kind,)
        if kind in SENIOR_KINDS
        else SENIOR_KINDS
    )
    answers = {
        code in SHOT_CLOCK_CLASSES[gender, age, team_kind]
        for team_kind in kinds
        if (gender, age, team_kind) in SHOT_CLOCK_CLASSES
    }
    return answers.pop() if len(answers) == 1 else None


def class_periods(context: RuleContext) -> tuple[int, ...] | None:
    """Return verified regulation periods for a class, or None when unverified.

    Senior and U19 A-category indoor matches last 2 x 25 minutes with a shot
    clock and 2 x 30 without (7.5.1, 9.3), so the class's shot clock decides.
    U17 indoor matches last 2 x 25 minutes either way.
    """
    if context.edition not in VERIFIED_EDITIONS:
        return None
    if context.known(context.age_group) in {"senior", "U19"}:
        shot_clock = class_shot_clock(context)
        if shot_clock is not None and context.known(context.discipline) == "indoor":
            return SHOT_CLOCK_PERIODS if shot_clock else RUNNING_PERIODS
    values = (
        context.known(context.category),
        context.known(context.playing_format),
        context.known(context.colour) or "",
        context.known(context.age_group),
        context.known(context.discipline),
    )
    return next(
        (
            periods
            for pattern, periods in CLASS_PERIODS
            if all(
                expected is None or expected == actual
                for expected, actual in zip(pattern, values, strict=True)
            )
        ),
        None,
    )


def official_periods(
    minutes: int | None, periods: Sequence[Mapping[str, Any]]
) -> tuple[tuple[int, ...] | None, tuple[AdditionalPeriod, ...], list[str]]:
    """Split provider periods into regulation and additional periods.

    Returns:
        Regulation minutes per period (None when invalid), additional periods,
        and the issues that invalidated the regulation periods.

    """
    regulation: list[int] = []
    additional: list[AdditionalPeriod] = []
    issues = []
    for period in periods:
        description = str(period.get("Description") or "")
        value = period.get("PlayTime")
        if type(value) is not int or value < 0:
            issues.append("invalid_period")
            continue
        key = description.casefold().strip()
        if key in ADDITIONAL or key in SHOOTOUT:
            additional.append(AdditionalPeriod(description, value))
        elif value > 0:
            regulation.append(value)
    if not regulation:
        issues.append("no_regulation_periods")
    elif minutes is not None and sum(regulation) != minutes:
        issues.append("periods_disagree_with_duration")
    if issues:
        return None, tuple(additional), issues
    return tuple(regulation), tuple(additional), []


def resolve_rules(
    context: RuleContext,
    *,
    minutes: int | None = None,
    periods: Sequence[Mapping[str, Any]] = (),
) -> MatchRules:
    """Build the profile; official periods win over class rules for timing."""
    version = rule_version(context.edition)
    verified = version is not None
    regulation, additional, issues = (
        official_periods(minutes, periods) if periods else (None, (), [])
    )
    evidence: dict[str, Any] = {
        "edition": context.edition,
        "category": context.known(context.category),
        "discipline": context.known(context.discipline),
        "age_group": context.known(context.age_group),
        "colour": context.known(context.colour),
        "playing_format": context.known(context.playing_format),
    }
    if regulation is not None:
        source = OFFICIAL_TIMING
        evidence["official_minutes"] = minutes
    else:
        regulation = class_periods(context)
        source = EDITION_RULES
        if issues:
            evidence["official_timing_rejected"] = issues
    category = context.known(context.category)
    form = context.known(context.playing_format)
    clock, shot_clock = _clock(context, regulation) if verified else (None, None)
    substitutions, substitution_limit = (
        (UNLIMITED, None)
        if verified and category == "b"
        else (LIMITED, A_SUBSTITUTIONS)
        if verified and category in {"a", "top"}
        else (UNKNOWN, None)
    )
    timeouts, timeout_limit = (
        (NOT_APPLICABLE, 0)
        if verified and category == "b"
        else (LIMITED, A_TIMEOUTS)
        if verified and category in {"a", "top"}
        else (UNKNOWN, None)
    )
    values: dict[str, Any] = {
        "periods": regulation,
        "players_per_team": {"four": 4, "eight": 8}.get(form or ""),
        "clock": clock,
        "shot_clock": shot_clock,
        "substitutions": None if substitutions == UNKNOWN else substitutions,
        "timeouts": None if timeouts == UNKNOWN else timeouts,
    }
    return MatchRules(
        source=source,
        rule_version=version,
        periods=regulation,
        additional_periods=additional,
        players_per_team=values["players_per_team"],
        clock=clock,
        shot_clock=shot_clock,
        substitutions=substitutions,
        substitution_limit=substitution_limit,
        timeouts=timeouts,
        timeout_limit=timeout_limit,
        unresolved=tuple(sorted(key for key, value in values.items() if value is None)),
        evidence=evidence,
    )


UNKNOWN_CLOCK: tuple[str | None, bool | None] = (None, None)


def _clock(
    context: RuleContext, regulation: tuple[int, ...] | None
) -> tuple[str | None, bool | None]:
    """Derive the clock type from verified rules; unknown combinations stay None."""
    if context.known(context.category) == "b":
        return "running", False
    shot_clock = class_shot_clock(context)
    if shot_clock is None:
        return _clock_from_duration(context, regulation)
    expected = class_periods(context)
    if regulation is not None and expected is not None and regulation != expected:
        # Official periods contradict the class rules: do not guess.
        return UNKNOWN_CLOCK
    return ("stopped", True) if shot_clock else ("running", False)


def _clock_from_duration(
    context: RuleContext, regulation: tuple[int, ...] | None
) -> tuple[str | None, bool | None]:
    """Infer the clock from regulation time when the class is not known."""
    category = context.known(context.category)
    age = context.known(context.age_group)
    if category == "a" and age == "U15":
        return "running", False
    indoor_u17 = age == "U17" and context.known(context.discipline) != "outdoor"
    # Indoor U17 plays 2 x 25 with a shot clock (stopped time) and without one
    # (running time), so its duration reveals nothing.
    if (
        indoor_u17
        or category not in {"a", "top"}
        or age not in {"senior", "U19", "U17"}
    ):
        return UNKNOWN_CLOCK
    return {
        SHOT_CLOCK_PERIODS: ("stopped", True),
        RUNNING_PERIODS: ("running", False),
    }.get(regulation or (), UNKNOWN_CLOCK)
