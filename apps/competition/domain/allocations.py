"""Read both KNKV allocation CSV layouts without guessing lost worksheet names."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
import re

from apps.competition.domain.classification import COLOURS, parse_label


A_HEADER = ["Poule/Team", "Dg", "Plaats"]
B_HEADER = ["Poule/Team", "Dg", "Lftd", "Punt", "Plaats"]


@dataclass(frozen=True)
class AllocationRow:
    """One original spreadsheet position and its aggregate team metadata."""

    row_number: int
    column: int
    section: str
    pool_name: str
    team_name: str
    city: str
    match_day: str
    average_age: Decimal | None
    knkv_points: Decimal | None
    classification: dict[str, str]


def section_context(section: str, category: str) -> dict[str, str]:
    """Read authoritative section headings, including the indoor exception.

    Raises:
        ValueError: A heading or format is unsupported.

    """
    result = {
        "discipline": "outdoor",
        "phase": "autumn",
        "category": category,
        "code": "b_senior",
        "age_group": "senior",
        "team_kind": "recreational",
        "playing_format": "eight",
    }
    if category == "a":
        return a_context(section, result)
    if section == "Midweek zaal":
        result.update(discipline="indoor", phase="indoor", team_kind="midweek")
    elif section in {"Midweek", "Midweek recreanten"}:
        result["team_kind"] = "midweek"
    elif section.startswith("G-korfbal"):
        result.update(code="adapted", age_group="unknown", team_kind="adapted")
    elif section.startswith("B-categorie "):
        colour = section.split()[1]
        if colour not in COLOURS:
            raise ValueError(f"Unknown allocation colour: {section}")
        result.update(
            code="youth_colour",
            age_group="youth",
            team_kind="youth",
            colour=COLOURS[colour],
        )
    elif section not in {"Senioren", "Senioren zaterdag", "Senioren zondag"}:
        raise ValueError(f"Unknown allocation heading: {section}")
    if "(4-tallen)" in section:
        result["playing_format"] = "four"
    elif "(" in section and "(8-tallen)" not in section:
        raise ValueError(f"Unknown playing format: {section}")
    return result


def number(value: str, *, age: bool) -> Decimal | None:
    """Preserve missing values, decimal commas and zero points.

    Raises:
        ValueError: A number is malformed or out of range.

    """
    if not value.strip():
        return None
    try:
        result = Decimal(value.strip().replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("Invalid allocation number") from error
    if (
        not result.is_finite()
        or result < 0
        or result > (100 if age else 99999)
        or result != result.quantize(Decimal("0.1" if age else "0.01"))
    ):
        raise ValueError("Allocation number out of range")
    return result


def read_file(content: bytes) -> tuple[date, list[list[str]], str, int]:
    """Identify the A/B column contract and original publication date.

    Raises:
        ValueError: Neither supported KNKV header matches.

    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    rows = list(csv.reader(StringIO(text), delimiter=";"))
    for category, header in (("a", A_HEADER), ("b", B_HEADER)):
        offset = len(header) + 2
        if (
            rows
            and len(rows[0]) == offset * 2 + 1
            and rows[0][1 : offset - 1] == header
            and rows[0][offset + 1 : offset * 2 - 1] == header
        ):
            day, month, year = map(int, rows[0][-1].strip().split("-"))
            return date(2000 + year, month, day), rows[1:], category, offset
    raise ValueError("Expected KNKV two-column A/B allocation CSV header")


def allocation_row(
    coordinate: tuple[int, int],
    section: str,
    pool: str,
    block: list[str],
    category: str,
) -> AllocationRow:
    """Validate one side independently so empty cells cannot shift memberships.

    Raises:
        ValueError: A team row has no established poule or heading.

    """
    index, offset = coordinate
    if not block[0].isdigit() or not block[1] or not pool or not section:
        raise ValueError(f"Unrecognized allocation row {index}, column {offset + 1}")
    return AllocationRow(
        index,
        offset + 1,
        section,
        pool,
        block[1],
        block[-1],
        block[2],
        number(block[3], age=True) if category == "b" else None,
        number(block[4], age=False) if category == "b" else None,
        section_context(section, category),
    )


def parse_allocations(content: bytes) -> tuple[date, list[AllocationRow]]:
    """Parse both columns and reset membership context at every heading.

    Raises:
        ValueError: Rows are malformed, duplicated or belong to unknown sections.

    """
    published, rows, category, width = read_file(content)
    section = ""
    pools = {0: "", width: ""}
    records = []
    identities = set()
    for index, raw_cells in enumerate(rows, 2):
        if len(raw_cells) != width * 2 + 1:
            raise ValueError(f"Incorrect column count on row {index}")
        cells = [cell.strip() for cell in raw_cells]
        if cells[0] and not cells[0].isdigit():
            section = read_section(cells)
            section_context(section, category)
            pools = {0: "", width: ""}
            continue
        for offset in (0, width):
            block = cells[offset : offset + width - 1]
            if not any(block):
                continue
            if (
                not block[0]
                and re.fullmatch(r"[A-Za-z0-9-]+", block[1])
                and not any(block[2:])
            ):
                pools[offset] = block[1]
                continue
            record = allocation_row(
                (index, offset), section, pools[offset], block, category
            )
            identity = (section, pools[offset], record.team_name.casefold())
            if identity in identities:
                raise ValueError(f"Duplicate team allocation on row {index}")
            identities.add(identity)
            records.append(record)
    if not records:
        raise ValueError("No allocation rows found")
    return published, records


def read_section(cells: list[str]) -> str:
    """Reject unexpected content beside a section title.

    Raises:
        ValueError: A title row contains unrecognized data.

    """
    if any(cells[1:]):
        raise ValueError("Unexpected content beside allocation heading")
    return cells[0]


def a_context(section: str, result: dict[str, str]) -> dict[str, str]:
    """Read the official A-category section, including reserve and youth classes.

    Raises:
        ValueError: An official class heading cannot be recognized.

    """
    parsed = parse_label(section.casefold())
    if "code" not in parsed:
        raise ValueError(f"Unknown A-category heading: {section}")
    result.update({"team_kind": "standard", **parsed})
    if result["code"] == "ereklasse" and result["team_kind"] == "standard":
        result["category"] = "top"
    return result
