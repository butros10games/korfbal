"""Optional human and calibrated-reader inputs for the match identity pass."""

from __future__ import annotations

import math

from .clip_match_identity import VERSION, Anchor, Calibration, NumberEvidence


MAX_INPUTS = 256
MAX_DIGITS = 2
MAX_NUMBERS = 100
MAX_ROSTER = 64
PROBABILITY_SUM_LIMIT = 1.000001


def validate(payload: object) -> dict:
    """Validate the versioned optional input without importing a vision runtime.

    Raises:
        ValueError: The input is malformed, unbounded or uses an unknown version.

    """
    if payload is None:
        return {"version": VERSION, "anchors": [], "numbers": [], "replay_shots": []}
    if not isinstance(payload, dict) or payload.get("version") != VERSION:
        raise ValueError("Match identity input requires version 1")
    allowed = {
        "version",
        "anchors",
        "numbers",
        "replay_shots",
        "calibration",
        "roster_size",
        "closed_set",
    }
    if set(payload) - allowed:
        raise ValueError("Unknown match identity input")
    for key in ("anchors", "numbers", "replay_shots"):
        values = payload.get(key, [])
        if not isinstance(values, list) or len(values) > MAX_INPUTS:
            raise ValueError("Match identity input exceeds the section bound")
    roster_size = payload.get("roster_size", 8)
    if (
        isinstance(roster_size, bool)
        or not isinstance(roster_size, int)
        or not 1 <= roster_size <= MAX_INPUTS
    ):
        raise ValueError("Choose an active roster size between 1 and 256")
    validate_anchor_rows(payload.get("anchors", []))
    validate_numbers(payload.get("numbers", []))
    for shot in payload.get("replay_shots", []):
        if isinstance(shot, bool) or not isinstance(shot, int) or shot < 0:
            raise ValueError("Replay shot IDs must be non-negative integers")
    validate_calibration(payload.get("calibration"))
    validate_closed_set(payload.get("closed_set"))
    return {
        "version": VERSION,
        "anchors": [],
        "numbers": [],
        "replay_shots": [],
        "roster_size": roster_size,
        **payload,
    }


def validate_anchor_rows(rows: list) -> None:
    """Validate bounded verified player names and optional roster numbers.

    Raises:
        ValueError: An anchor cannot name a player safely.

    """
    for row in rows:
        if not isinstance(row, dict) or set(row) - {
            "identity",
            "player_id",
            "team",
            "number",
        }:
            raise ValueError("Invalid identity anchor")
        if any(
            not isinstance(row.get(k), str) or not row[k] or len(row[k]) > MAX_INPUTS
            for k in ("identity", "player_id", "team")
        ) or row["team"] not in {"team_a", "team_b"}:
            raise ValueError("An anchor needs an identity, player and team")
        number = row.get("number")
        if number is not None and (
            not isinstance(number, str)
            or not number.isdigit()
            or len(number) > MAX_DIGITS
        ):
            raise ValueError("An anchor number must have one or two digits")


def probability(value: object) -> bool:
    """Accept only finite JSON probabilities, excluding booleans."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def validate_numbers(rows: list) -> None:
    """Bound calibrated distributions and require independent episode provenance.

    Raises:
        ValueError: A number distribution is malformed or lacks calibration evidence.

    """
    for row in rows:
        if not isinstance(row, dict) or set(row) - {
            "identity",
            "episode",
            "probabilities",
            "readability",
            "calibrated",
            "calibration_id",
        }:
            raise ValueError("Invalid shirt-number evidence")
        if any(
            not isinstance(row.get(k), str) or not row[k] or len(row[k]) > MAX_INPUTS
            for k in ("identity", "episode")
        ):
            raise ValueError(
                "Number evidence needs an identity and independent episode"
            )
        values = row.get("probabilities")
        if not isinstance(values, dict) or len(values) > MAX_NUMBERS or not values:
            raise ValueError("Number evidence needs a bounded probability distribution")
        if (
            any(
                not isinstance(k, str)
                or not k.isdigit()
                or len(k) > MAX_DIGITS
                or not probability(v)
                for k, v in values.items()
            )
            or sum(values.values()) > PROBABILITY_SUM_LIMIT
        ):
            raise ValueError("Invalid number probabilities")
        if not probability(row.get("readability")) or not isinstance(
            row.get("calibrated"), bool
        ):
            raise ValueError("Invalid number readability or calibration flag")
        if row["calibrated"] and (
            not isinstance(row.get("calibration_id"), str)
            or not row["calibration_id"]
            or len(row["calibration_id"]) > MAX_INPUTS
        ):
            raise ValueError("Calibrated number evidence needs its calibration ID")


def validate_calibration(value: object) -> None:
    """Require monotone finite empirical bins and explicit calibration provenance.

    Raises:
        ValueError: The supplied calibration cannot be interpreted safely.

    """
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) != {"provenance", "bins"}
        or not isinstance(value["provenance"], str)
        or not value["provenance"]
    ):
        raise ValueError("Appearance calibration needs provenance and bins")
    bins = value["bins"]
    if not isinstance(bins, list) or len(bins) > MAX_INPUTS:
        raise ValueError("Invalid appearance calibration bins")
    previous = (-1.0, 0.0)
    for row in bins:
        if (
            not isinstance(row, list)
            or len(row) != MAX_DIGITS
            or not all(probability(v) for v in row)
            or row[0] <= previous[0]
            or row[1] < previous[1]
        ):
            raise ValueError("Appearance calibration bins must increase monotonically")
        previous = row


def anchors(payload: dict) -> list[Anchor]:
    """Construct verified anchors from a validated contract."""
    return [Anchor(**row) for row in payload["anchors"]]


def calibration(payload: dict) -> Calibration | None:
    """Construct an optional empirical calibrator; no automatic default is claimed."""
    value = payload.get("calibration")
    return (
        Calibration(value["provenance"], tuple(tuple(row) for row in value["bins"]))
        if value
        else None
    )


def readings(payload: dict) -> dict[str, list[NumberEvidence]]:
    """Expose the calibrated-reader integration without importing its implementation."""
    output: dict[str, list[NumberEvidence]] = {}
    for row in payload["numbers"]:
        output.setdefault(row["identity"], []).append(
            NumberEvidence(
                row["episode"],
                row["probabilities"],
                row["readability"],
                row["calibrated"],
            )
        )
    return output


def validate_closed_set(value: object) -> None:
    """Validate the opt-in roster classifier without importing NumPy or SciPy.

    Raises:
        ValueError: The roster or empirical calibration is malformed.

    """
    if value is None:
        return
    if not isinstance(value, dict) or set(value) - {
        "roster",
        "input",
        "calibration",
        "numbers",
        "orientation",
        "propagate",
    }:
        raise ValueError("Invalid closed-set identity options")
    if value.get("input", "tracklets") not in {"tracklets", "linked"}:
        raise ValueError("Choose tracklets or linked identities")
    if value.get("numbers", "automatic") not in {"automatic", "off"}:
        raise ValueError("Shirt-number anchors are automatic or off")
    if value.get("orientation", "automatic") not in {"automatic", "kits"}:
        raise ValueError("Roster sides are oriented automatically or are kits")
    if not isinstance(value.get("propagate", False), bool):
        raise ValueError("Linked-name propagation is a boolean")
    roster = value.get("roster")
    # An empty roster is filled by reviewers (team and shirt number) while naming.
    if not isinstance(roster, list) or len(roster) > MAX_ROSTER:
        raise ValueError("Closed-set identification needs a roster of 0 to 64 players")
    validate_roster(roster)
    calibration = value.get("calibration")
    if calibration is not None:
        if not isinstance(calibration, dict) or set(calibration) != {
            "provenance",
            "descriptor_recipe",
            "bins",
        }:
            raise ValueError("Closed-set calibration needs recipe, provenance and bins")
        if (
            not isinstance(calibration["descriptor_recipe"], str)
            or not 1 <= len(calibration["descriptor_recipe"]) <= MAX_INPUTS
        ):
            raise ValueError("Calibration needs its appearance recipe")
        validate_calibration({
            "provenance": calibration["provenance"],
            "bins": calibration["bins"],
        })


def validate_roster(roster: list) -> None:
    """Require unique player IDs and canonical team-scoped shirt numbers.

    Raises:
        ValueError: The roster is ambiguous or malformed.

    """
    seen = set()
    numbers = set()
    for row in roster:
        if not isinstance(row, dict) or set(row) - {"player_id", "team", "number"}:
            raise ValueError("Invalid roster player")
        validate_anchor_rows([{**row, "identity": "roster"}])
        if row["player_id"] in seen:
            raise ValueError("Duplicate match roster player")
        seen.add(row["player_id"])
        number = row.get("number")
        if number is not None:
            if str(int(number)) != number or (row["team"], number) in numbers:
                raise ValueError(
                    "Roster shirt numbers must be canonical and team-unique"
                )
            numbers.add((row["team"], number))
