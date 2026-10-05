"""Conservative KNKV label mapping, with season-scoped rules and no ratings.

Sources: https://www.knkv.nl/kennisbank/competitiehandboek/
https://www.knkv.nl/kennisbank/toolkit-herstructurering-competitie/
Unknown provider labels stay unresolved; pool numbers never imply strength.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import re


VERSION = "knkv-classification-v2"
UNKNOWN = "unknown"
MODERN_YOUTH_YEAR = 2025
REDUCED_CLASSES_YEAR = 2026
# Raw provider values need a reviewed calibration before they select rules.
SOURCE_GENDERS: dict[str, str] = {}
HANDBOOK = "https://www.knkv.nl/kennisbank/competitiehandboek/"
INDOOR_CLASSES = "https://www.knkv.nl/competitie-indeling-zaal-2026-2027-deels-bekend/"
# A season whose korfbal year cannot be determined selects no edition rules.
UNRESOLVED_EDITION = "unresolved_edition"
COLOURS = {
    "rood": "red",
    "oranje": "orange",
    "geel": "yellow",
    "groen": "green",
    "blauw": "blue",
}
CLASS_NAMES = {
    "korfbal league": "league",
    "korfbal league 2": "league_2",
    "ereklasse": "ereklasse",
    "hoofdklasse": "hoofdklasse",
    "overgangsklasse": "overgangsklasse",
    "topklasse": "topklasse",
    **{f"{n}e klasse": f"class_{n}" for n in range(1, 5)},
}
CHOICES = {
    "discipline": {UNKNOWN, "indoor", "outdoor"},
    "phase": {UNKNOWN, "indoor", "autumn", "spring", "full_season"},
    "gender": {UNKNOWN, "mixed", "women"},
    "category": {UNKNOWN, "top", "a", "b"},
    "age_group": {
        UNKNOWN,
        "senior",
        "U19",
        "U17",
        "U15",
        "youth",
        "A",
        "B",
        "C",
        "D",
        "E",
        "F",
    },
    "team_kind": {
        UNKNOWN,
        "standard",
        "reserve",
        "youth",
        "recreational",
        "midweek",
        "adapted",
    },
    "colour": {UNKNOWN, *COLOURS.values()},
    "playing_format": {UNKNOWN, "four", "eight"},
    "code": {UNKNOWN, *CLASS_NAMES.values(), "youth_colour", "b_senior", "adapted"},
}


@dataclass(frozen=True)
class Classification:
    """Explicit unknowns allow partial mappings without fabricated metadata."""

    discipline: str = UNKNOWN
    phase: str = UNKNOWN
    gender: str = UNKNOWN
    code: str = UNKNOWN
    category: str = UNKNOWN
    age_group: str = UNKNOWN
    team_kind: str = UNKNOWN
    colour: str = UNKNOWN
    playing_format: str = UNKNOWN


def designation(name: str, season_start: int | None) -> dict[str, object]:
    """Read only the terminal designation; J numbers do not encode ages.

    ``season_start`` is the edition (start year of the korfbal year), not the
    calendar year of a playing season: spring 2025 belongs to 2024-2025.
    """
    match = re.search(
        r"(?:^|\s)(U(?:19|17|15)|J|[A-F])\s*-?\s*(\d+)$", name, re.IGNORECASE
    )
    if not match:
        return {
            "kind": "unknown",
            "number": None,
            "age_group": None,
            "average_age": None,
        }
    prefix, number = match.group(1).upper(), int(match.group(2))
    if season_start is None:
        return {
            "kind": "unknown",
            "number": number,
            "age_group": None,
            "average_age": None,
        }
    modern = season_start >= MODERN_YOUTH_YEAR
    kind = (
        "j"
        if modern and prefix == "J"
        else "u"
        if modern and prefix.startswith("U")
        else "historical"
        if not modern and len(prefix) == 1 and prefix != "J"
        else "unknown"
    )
    return {
        "kind": kind,
        "number": number,
        "age_group": prefix if kind in {"u", "historical"} else None,
        "average_age": None,
    }


def classify(
    label: str,
    sport: str,
    season_start: int | None,
    override: dict[str, str] | None = None,
    *,
    context: dict[str, str] | None = None,
) -> tuple[Classification, list[str]]:
    """Normalize an entire class label; never consume a pool code as a class.

    ``season_start`` is the edition year that selects season-scoped rules;
    None (an unresolved edition) applies no year-specific rule at all.

    Invalid context or reviewed override values raise ValueError.

    """
    values = asdict(Classification())
    if re.fullmatch(r"KORFBALL-(ZA|VE)-(WK|BK)", sport):
        values["discipline"] = "indoor" if "-ZA-" in sport else "outdoor"
        if values["discipline"] == "indoor":
            values["phase"] = "indoor"
    text = " ".join(label.casefold().split())
    for word, replacement in (
        ("eerste", "1e"),
        ("tweede", "2e"),
        ("derde", "3e"),
        ("vierde", "4e"),
    ):
        text = text.replace(word, replacement)
    text = re.sub(r"\b([1-4])(?:ste|de)\b", r"\1e", text)
    values.update(provider_label(text, values["discipline"]))
    context_issues = merge_context(values, context or {})
    context_issues.extend(merge_override(values, override or {}, context or {}))
    complete_known_context(
        values, {**(context or {}), **(override or {})}, season_start
    )
    result = Classification(**values)
    issues = list(dict.fromkeys([*context_issues, *validate(result, season_start)]))
    return result, issues


def merge_context(values: dict[str, str], context: dict[str, str]) -> list[str]:
    """Fill unknown fields from source context and report explicit disagreements.

    Raises:
        ValueError: A context field or value is unsupported.

    """
    context_issues = []
    for field, value in context.items():
        if field not in CHOICES or value not in CHOICES[field]:
            raise ValueError(f"Unsupported classification context: {field}")
        if value == UNKNOWN:
            continue
        if values[field] not in {UNKNOWN, value}:
            context_issues.append(
                "conflicting_period" if field == "phase" else f"conflicting_{field}"
            )
        else:
            values[field] = value
    return context_issues


def merge_override(
    values: dict[str, str], override: dict[str, str], context: dict[str, str]
) -> list[str]:
    """Apply reviewed values while retaining a conflicting source period.

    Raises:
        ValueError: An override field or value is unsupported.

    """
    context_issues = []
    for field, value in override.items():
        if field not in CHOICES or value not in CHOICES[field]:
            raise ValueError(f"Unsupported classification override: {field}")
        if field == "phase" and context.get(field, UNKNOWN) not in {
            UNKNOWN,
            value,
        }:
            context_issues.append("conflicting_period")
        values[field] = value
    return context_issues


@dataclass(frozen=True)
class LadderRule:
    """A sourced structure; identity is stable when another edition verifies it."""

    first_edition: int
    last_edition: int
    discipline: str
    gender: str
    age_group: str
    team_kind: str
    codes: tuple[str, ...]
    sources: tuple[str, ...]

    @property
    def ladder_id(self) -> str:
        """Separate lanes without making every edition a different ladder."""
        return f"{self.discipline}:{self.gender}:{self.age_group}:{self.team_kind}"

    @property
    def hierarchy_revision(self) -> str:
        """Equal ordered structures remain comparable across sourced editions."""
        return sha256("|".join(self.codes).encode()).hexdigest()[:16]


def _verified_rules() -> tuple[LadderRule, ...]:
    """2026-27 handbook 2.1 and the indoor restructuring announcement.

    The current handbook cannot verify historical A-F or earlier senior ladders.
    Indoor Ereklasse has no demonstrated context and is deliberately absent.
    """
    rules = []
    for discipline in ("indoor", "outdoor"):
        for kind in ("standard", "reserve"):
            top = ("league", "league_2") if discipline == "indoor" else ("ereklasse",)
            mixed = (
                *top,
                "hoofdklasse",
                "overgangsklasse",
                "class_1",
                "class_2",
                "class_3",
            )
            if kind == "standard":
                mixed = (*mixed, "class_4")
            for gender, codes in (
                ("mixed", mixed),
                ("women", ("topklasse", "hoofdklasse", "overgangsklasse", "class_1")),
            ):
                rules.append(
                    LadderRule(
                        2026,
                        2026,
                        discipline,
                        gender,
                        "senior",
                        kind,
                        codes,
                        (HANDBOOK, INDOOR_CLASSES),
                    )
                )
        for gender in ("mixed", "women"):
            for age in ("U19", "U17", "U15"):
                codes = ("hoofdklasse",)
                if gender == "mixed" and age == "U19":
                    codes = (*codes, "overgangsklasse")
                if gender == "mixed" or age == "U19":
                    codes = (*codes, "class_1")
                rules.append(
                    LadderRule(
                        2026,
                        2026,
                        discipline,
                        gender,
                        age,
                        "youth",
                        codes,
                        (HANDBOOK, INDOOR_CLASSES),
                    )
                )
    return tuple(rules)


LADDER_RULES = _verified_rules()


def hierarchy(value: Classification, edition: int | None) -> LadderRule | None:
    """Return an edition-appropriate verified ladder, never guess an old one."""
    if edition is None or value.category == "b":
        return None
    return next(
        (
            rule
            for rule in LADDER_RULES
            if rule.first_edition <= edition <= rule.last_edition
            and (rule.discipline, rule.gender, rule.age_group, rule.team_kind)
            == (value.discipline, value.gender, value.age_group, value.team_kind)
        ),
        None,
    )


def validate(value: Classification, year: int | None) -> list[str]:
    """Report missing context and season conflicts instead of guessing it."""
    issues = []
    for field in (
        "discipline",
        "phase",
        "gender",
        "code",
        "category",
        "age_group",
        "team_kind",
        "playing_format",
    ):
        if getattr(value, field) == UNKNOWN:
            issues.append(f"missing_{field}")
    if (
        value.discipline == "indoor"
        and value.phase not in {UNKNOWN, "indoor", "full_season"}
    ) or (value.discipline == "outdoor" and value.phase == "indoor"):
        issues.append("conflicting_phase")
    if year is None:
        issues.append(UNRESOLVED_EDITION)
    elif (
        year < MODERN_YOUTH_YEAR
        and (value.age_group.startswith("U") or value.code == "youth_colour")
    ) or (year >= MODERN_YOUTH_YEAR and value.age_group in set("ABCDEF")):
        issues.append("age_scheme_season_conflict")
    if value.code == "youth_colour" and (
        value.colour == UNKNOWN
        or value.category != "b"
        or value.team_kind != "youth"
        or value.age_group != "youth"
    ):
        issues.append("conflicting_youth_colour")
    ladder = hierarchy(value, year)
    if ladder and value.code not in {*ladder.codes, UNKNOWN}:
        issues.append("class_not_in_hierarchy")
    reduced = year == REDUCED_CLASSES_YEAR
    if (
        reduced
        and value.gender == "mixed"
        and value.age_group in {"U19", "U17", "U15"}
        and value.code == "class_2"
    ):
        issues.append("class_removed_for_season")
    if (
        reduced
        and value.gender == "mixed"
        and value.team_kind == "reserve"
        and value.code == "class_4"
    ):
        issues.append("class_removed_for_season")
    issues.extend(context_conflicts(value))
    return issues


def level(
    value: Classification, issues: list[str], edition: int | None = None
) -> int | None:
    """Expose official hierarchy position only with sufficient consistent context."""
    ladder = hierarchy(value, edition)
    if level_reason(value, issues, edition) != "ranked" or ladder is None:
        return None
    return ladder.codes.index(value.code) + 1


def level_reason(value: Classification, issues: list[str], edition: int | None) -> str:
    """Explain class identity separately from numeric-ladder eligibility."""
    if any(not issue.startswith("missing_") for issue in issues):
        return "conflict"
    if value.category == "b" or value.code in {"b_senior", "adapted", "youth_colour"}:
        return "no_ladder_b_category"
    for field in ("discipline", "gender", "age_group", "team_kind", "code"):
        if getattr(value, field) == UNKNOWN:
            return f"missing_{field}"
    rule = hierarchy(value, edition)
    if rule is None:
        return "ladder_unverified"
    return "ranked" if value.code in rule.codes else "conflict"


def ladder_context(
    value: Classification, issues: list[str], edition: int | None
) -> dict[str, object]:
    """Additive public metadata for compatible history comparisons."""
    rule = hierarchy(value, edition)
    return {
        "ladder_id": rule.ladder_id if rule else None,
        "hierarchy_revision": rule.hierarchy_revision if rule else None,
        "level_reason": level_reason(value, issues, edition),
        "level": level(value, issues, edition),
    }


def parse_label(text: str) -> dict[str, str]:
    """Read explicit label tokens; never borrow a designation from one member."""
    values = {}
    # Prefixes are explicit provider label evidence, not inferred from team names.
    prefixes = (
        ("dames", "gender", "women"),
        ("gemengd", "gender", "mixed"),
        ("reserve", "team_kind", "reserve"),
        ("senioren", "age_group", "senior"),
    )
    changed = True
    while changed:
        changed = False
        for prefix, field, value in prefixes:
            if text.startswith(prefix + " "):
                values[field] = value
                text = text[len(prefix) :].strip()
                changed = True
    age = re.match(r"^(u19|u17|u15|[a-f](?:-jeugd| jeugd))\s+", text)
    if age:
        token = age.group(1).upper()
        values["age_group"] = token if token.startswith("U") else token[0]
        values["team_kind"] = "youth"
        text = text[age.end() :]
    if text in COLOURS:
        values.update(
            code="youth_colour",
            category="b",
            age_group="youth",
            team_kind="youth",
            colour=COLOURS[text],
        )
    elif text in CLASS_NAMES:
        values["code"] = CLASS_NAMES[text]
    return values


def complete_known_context(
    values: dict[str, str], override: dict[str, str] | None, edition: int | None
) -> None:
    """Derive format/category only from an explicit age and competition context."""
    if (
        edition == REDUCED_CLASSES_YEAR
        and values["team_kind"] == "reserve"
        and values["age_group"] == UNKNOWN
    ):
        values["age_group"] = "senior"
    if values["age_group"] in {"senior", "U19", "U17", "U15"} and values[
        "code"
    ] not in {UNKNOWN, "youth_colour", "b_senior", "adapted"}:
        values["playing_format"] = (
            values["playing_format"]
            if override and "playing_format" in override
            else "eight"
        )
        if values["category"] == UNKNOWN and values["gender"] != UNKNOWN:
            values["category"] = "a"
            if (
                values["age_group"] == "senior"
                and values["gender"] == "mixed"
                and (
                    values["code"] in {"league", "league_2"}
                    or (
                        values["code"] == "ereklasse"
                        and values["team_kind"] == "standard"
                    )
                )
            ):
                values["category"] = "top"


def context_conflicts(value: Classification) -> list[str]:
    """Reject impossible combinations even when every field was supplied."""
    issues = []
    if value.age_group in {
        "U19",
        "U17",
        "U15",
        "youth",
        "A",
        "B",
        "C",
        "D",
        "E",
        "F",
    } and value.team_kind not in {UNKNOWN, "youth"}:
        issues.append("conflicting_age_team_kind")
    if value.age_group == "senior" and value.team_kind == "youth":
        issues.append("conflicting_age_team_kind")
    if value.code in CLASS_NAMES.values() and value.playing_format not in {
        UNKNOWN,
        "eight",
    }:
        issues.append("conflicting_class_playing_format")
    if value.code in {"b_senior", "adapted", "youth_colour"} and value.category not in {
        UNKNOWN,
        "b",
    }:
        issues.append("conflicting_category")
    if value.category == "b" and value.code in CLASS_NAMES.values():
        issues.append("conflicting_category")
    return issues


def provider_label(text: str, discipline: str) -> dict[str, str]:
    """Normalize observed Sportlink autumn labels against KNKV worksheet headings."""
    context = {}
    if text.endswith(" nj"):
        text = text[:-3].strip()
        if discipline == "outdoor":
            context["phase"] = "autumn"
    if " dames" in text:
        text = text.replace(" dames", "")
        context["gender"] = "women"
    colour = re.fullmatch(r"b(4)?-(rood|oranje|geel|groen|blauw)", text)
    if colour:
        return {
            **context,
            "code": "youth_colour",
            "category": "b",
            "age_group": "youth",
            "team_kind": "youth",
            "colour": COLOURS[colour.group(2)],
            "playing_format": "four" if colour.group(1) else "eight",
        }
    special = {
        "senioren bk": ("b_senior", "senior", "recreational", "eight"),
        "midweek veld": ("b_senior", "senior", "midweek", "eight"),
        "midweek zaal": ("b_senior", "senior", "midweek", "eight"),
        "midweek": ("b_senior", "senior", "midweek", "eight"),
        "midweek recreanten": ("b_senior", "senior", "midweek", "eight"),
        "g-korfbal toernooi": ("adapted", UNKNOWN, "adapted", "eight"),
        "g4-korfbal": ("adapted", UNKNOWN, "adapted", "four"),
        "g4-korfbal toernooi": ("adapted", UNKNOWN, "adapted", "four"),
    }
    if text == "midweek zaal":
        context.update(discipline="indoor", phase="indoor")
    if text in special:
        code, age, kind, playing_format = special[text]
        return {
            **context,
            "code": code,
            "category": "b",
            "age_group": age,
            "team_kind": kind,
            "playing_format": playing_format,
        }
    return {**context, **parse_label(text)}
