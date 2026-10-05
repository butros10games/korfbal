"""Pure response integrity and conservative fixture coverage contracts."""

from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from apps.competition.domain.source_context import merge_duplicate_context


STANDING_FIELDS = (
    "Position",
    "TotalMatches",
    "Won",
    "Draw",
    "Lost",
    "TotalPoints",
    "PenaltyPoints",
    "GoalsFor",
    "GoalsAgainst",
    "GoalsDifference",
)


def is_self_fixture(row: dict[str, Any]) -> bool:
    """Identify an unusable fixture before its dates influence pool routing."""
    return str(row["HomeTeam"]["PublicTeamId"]) == str(row["AwayTeam"]["PublicTeamId"])


def match_identity(row: dict[str, Any], *, result: bool) -> dict[str, Any]:
    """Normalize duplicate comparison; leave invalid rows to routing diagnostics."""
    raw_stamp = str(row.get("MatchDateTime") or "")
    try:
        stamp = datetime.fromisoformat(raw_stamp)
    except ValueError:
        stamp = None
    kickoff = (
        stamp.astimezone(UTC)
        if stamp is not None and stamp.tzinfo is not None
        else raw_stamp
    )
    fields = {"starts_at": kickoff, "status": row["Status"]}
    for side in ("HomeTeam", "AwayTeam"):
        team = row[side]
        fields[side] = (
            str(team["PublicTeamId"]),
            str(team["Club"]["ClubId"]),
            team.get("SportId") or "",
        )
    if result:
        fields.update(
            home_score=(row.get("HomeResult") or {}).get("Score"),
            away_score=(row.get("AwayResult") or {}).get("Score"),
            automatic_result=deepcopy(row.get("AutoResult")),
        )
    fields["pool"] = str(row["Pool"]["PoolId"]) if row.get("Pool") else None
    return fields


def compare_match(previous: dict[str, Any], current: dict[str, Any]) -> None:
    """Reject contradictions while allowing a missing pool to be supplied.

    Raises:
        ValueError: Duplicate fixture identity or result fields disagree.

    """
    if any(
        previous[key] != value for key, value in current.items() if key != "pool"
    ) or (
        previous["pool"] is not None
        and current["pool"] is not None
        and previous["pool"] != current["pool"]
    ):
        raise ValueError("Conflicting duplicate match identity or result")


def unique_matches(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate every copy before routing; return owned, enriched normalized rows."""
    accepted: dict[str, dict[str, Any]] = {}
    identities: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["PublicMatchId"])
        identity = match_identity(row, result=True)
        if key in identities:
            compare_match(identities[key], identity)
            accepted[key] = merge_duplicate_match(accepted[key], row)
            if identities[key]["pool"] is None and identity["pool"] is not None:
                identities[key]["pool"] = identity["pool"]
        else:
            identities[key], accepted[key] = identity, deepcopy(row)
    return list(accepted.values())


def merge_duplicate_match(
    first: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Retain validated additional nonpersonal context from identical fixture copies."""
    merged = merge_duplicate_context("match", first, incoming)
    for side in ("HomeTeam", "AwayTeam"):
        merged[side] = merge_duplicate_context("team", first[side], incoming[side])
    if incoming.get("Pool"):
        merged["Pool"] = merge_duplicate_context(
            "pool", first.get("Pool") or incoming["Pool"], incoming["Pool"]
        )
    return merged


def unique_standings(
    rows: Iterable[dict[str, Any]],
    *,
    require_counts: bool = True,
) -> list[dict[str, Any]]:
    """Validate official membership and played-count proof before any writes.

    Raises:
        ValueError: A count is invalid or a repeated team has contradictory values.

    """
    accepted: dict[str, dict[str, Any]] = {}
    for row in rows:
        total = row.get("TotalMatches")
        if (require_counts or "TotalMatches" in row) and (
            isinstance(total, bool) or not isinstance(total, int) or total < 0
        ):
            raise ValueError("Standing TotalMatches must be a nonnegative integer")
        key = str(row["PublicTeamId"])
        if key in accepted:
            previous = accepted[key]

            def identity(item: dict[str, Any]) -> tuple:
                return (
                    str(item["Club"]["ClubId"]),
                    item.get("SportId") or "",
                    {field: item[field] for field in STANDING_FIELDS if field in item},
                )

            if identity(previous) != identity(row):
                raise ValueError(
                    "Conflicting duplicate standing team identity or values"
                )
            accepted[key] = merge_duplicate_context("team", previous, row)
        else:
            accepted[key] = merge_duplicate_context("team", row, row)
    return list(accepted.values())


def standing_projection(table: dict[str, Any] | None) -> dict[str, Any] | None:
    """Retain public table evidence only; absence is distinct from an empty table."""
    if table is None:
        return None
    rows = unique_standings(table.get("PoolStandingTeam") or [])
    return {
        "PoolStandingTeam": [
            {
                **{
                    key: row[key]
                    for key in (
                        "PublicTeamId",
                        "TeamName",
                        "SportId",
                        "Gender",
                        "TeamCode",
                        "SportDescription",
                        "SportTag",
                        "SortOrder",
                        "LocalTeam",
                    )
                    if key in row
                },
                **(
                    {
                        "Class": [
                            {
                                key: item[key]
                                for key in ("ClassId", "ClassName")
                                if key in item
                            }
                            for item in row["Class"]
                        ]
                    }
                    if isinstance(row.get("Class"), list)
                    else {}
                ),
                "Club": {
                    key: row["Club"][key]
                    for key in ("ClubId", "ClubName", "City")
                    if key in row["Club"]
                },
                **{key: row[key] for key in STANDING_FIELDS if key in row},
            }
            for row in rows
        ]
    }


def fixture_coverage(
    fixtures: Iterable[tuple[str, str, bool]],
    expected: dict[str, Any],
    *,
    members: set[str],
    unfiltered: bool,
    accounted: bool = True,
) -> tuple[bool, dict[str, Any]]:
    """Compare a unique persisted schedule with independently supplied counts."""
    observed: Counter[str] = Counter()
    rows = list(fixtures)
    nonfinal = 0
    for home, away, final in rows:
        if final:
            observed.update((home, away))
        else:
            nonfinal += 1
    known = (
        bool(expected)
        and members == set(expected)
        and all(
            isinstance(total, int) and not isinstance(total, bool) and total >= 0
            for total in expected.values()
        )
    )
    complete = (
        accounted
        and unfiltered
        and known
        and bool(rows)
        and not nonfinal
        and set(observed) <= set(expected)
        and all(observed[team] == total for team, total in expected.items())
    )
    return complete, {
        "matches": len(rows),
        "expected_played": expected,
        "observed_played": dict(observed),
        "nonfinal_matches": nonfinal,
        "source_accounted": accounted,
    }
