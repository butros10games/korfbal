"""Conservative KNKV label mapping, with season-scoped rules and no ratings.

Sources: https://www.knkv.nl/kennisbank/competitiehandboek/
https://www.knkv.nl/kennisbank/toolkit-herstructurering-competitie/
Unknown provider labels stay unresolved; pool numbers never imply strength.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re


VERSION = "knkv-classification-v1"
UNKNOWN = "unknown"
MODERN_YOUTH_YEAR = 2025
REDUCED_CLASSES_YEAR = 2026
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


def designation(name: str, season_start: int) -> dict[str, object]:
    """Read only the terminal designation; J numbers do not encode ages."""
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
    label: str, sport: str, season_start: int, override: dict[str, str] | None = None
) -> tuple[Classification, list[str]]:
    """Normalize an entire class label; never consume a pool code as a class.

    Raises:
        ValueError: A reviewed override contains unsupported fields or values.

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
    if override:
        for field, value in override.items():
            if field not in CHOICES or value not in CHOICES[field]:
                raise ValueError(f"Unsupported classification override: {field}")
            values[field] = value
    complete_known_context(values, override)
    result = Classification(**values)
    issues = validate(result, season_start)
    return result, issues


def hierarchy(value: Classification) -> tuple[str, ...]:
    """Return only a known competition ladder, never a cross-context ranking."""
    if UNKNOWN in {value.gender, value.discipline}:
        return ()
    if value.age_group == "senior" and value.team_kind in {"standard", "reserve"}:
        if value.gender == "women":
            return ("topklasse", "hoofdklasse", "overgangsklasse", "class_1")
        top = ("league", "league_2") if value.discipline == "indoor" else ("ereklasse",)
        return (
            *top,
            "hoofdklasse",
            "overgangsklasse",
            "class_1",
            "class_2",
            "class_3",
            "class_4",
        )
    if value.team_kind == "youth" and value.age_group in {"U19", "U17", "U15"}:
        return (
            ("hoofdklasse", "overgangsklasse", "class_1", "class_2")
            if value.age_group == "U19" and value.gender == "mixed"
            else ("hoofdklasse", "class_1", "class_2")
        )
    return ()


def validate(value: Classification, year: int) -> list[str]:
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
    if (
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
    ladder = hierarchy(value)
    if ladder and value.code not in {*ladder, UNKNOWN}:
        issues.append("class_not_in_hierarchy")
    if (
        year >= REDUCED_CLASSES_YEAR
        and value.gender == "mixed"
        and value.age_group in {"U19", "U17", "U15"}
        and value.code == "class_2"
    ):
        issues.append("class_removed_for_season")
    if (
        year >= REDUCED_CLASSES_YEAR
        and value.gender == "mixed"
        and value.team_kind == "reserve"
        and value.code == "class_4"
    ):
        issues.append("class_removed_for_season")
    issues.extend(context_conflicts(value))
    return issues


def level(value: Classification, issues: list[str]) -> int | None:
    """Expose official hierarchy position only with sufficient consistent context."""
    ladder = hierarchy(value)
    if issues:
        return None
    if value.code not in ladder:
        return None
    return ladder.index(value.code) + 1


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
    values: dict[str, str], override: dict[str, str] | None
) -> None:
    """Derive format/category only from an explicit age and competition context."""
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
    if (
        value.gender == "women"
        and value.age_group in {"U17", "U15"}
        and value.code not in {UNKNOWN, "hoofdklasse"}
    ):
        issues.append("class_not_in_hierarchy")
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
