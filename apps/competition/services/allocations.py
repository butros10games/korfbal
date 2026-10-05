"""Stage KNKV allocations and reconcile exact identities without creating teams."""

from collections import Counter, defaultdict
from dataclasses import asdict
from hashlib import sha256
import re
from typing import Any

from django.db import transaction

from apps.competition.domain.allocations import parse_allocations
from apps.competition.domain.classification import classify
from apps.competition.models import Allocation, AllocationSource, Pool, PoolEntry
from apps.competition.services.class_alignment import (
    allocation_class,
    realign_pool_allocations,
)
from apps.competition.services.classification import map_pool
from apps.competition.services.competition_periods import resolve_pool_periods
from apps.competition.services.reconciliation import normalized
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_edition


__all__ = [
    "allocation_class",
    "import_allocations",
    "pool_key",
    "realign_pool_allocations",
    "team_key",
]


@transaction.atomic
def import_allocations(
    content: bytes, season: Season, *, apply: bool, label: str, gender: str = "unknown"
) -> dict[str, Any]:
    """Retain the entire file and link only unambiguous existing memberships.

    Raises:
        ValueError: The snapshot date, gender or file contract is invalid.

    """
    if gender not in {"unknown", "mixed", "women"}:
        raise ValueError("Invalid gender scope")
    published, rows = parse_allocations(content)
    if not season.start_date <= published <= season.end_date:
        raise ValueError("Allocation publication date is outside the selected season")
    digest = sha256(content).hexdigest()
    candidates = defaultdict(list)
    for entry in PoolEntry.objects.filter(
        pool__season=season, team__season=season
    ).select_related("pool__season", "team__club"):
        key = (
            pool_key(entry.pool.name),
            team_key(entry.team.name),
        )
        candidates[key].append(entry)
    decisions = []
    source = AllocationSource.objects.filter(season=season, digest=digest).first()
    if apply:
        source, _ = AllocationSource.objects.get_or_create(
            season=season,
            digest=digest,
            defaults={"label": label, "published_on": published},
        )
        source = AllocationSource.objects.select_for_update(no_key=True).get(
            pk=source.pk
        )
    existing = (
        {
            (item.row_number, item.column): item
            for item in Allocation.objects.filter(source=source)
        }
        if source
        else {}
    )
    changed = 0
    classes = {}
    affected = {}
    for row in rows:
        if row.classification.get("gender", gender) != gender:
            raise ValueError("File heading conflicts with the supplied gender scope")
        key = (
            pool_key(row.pool_name),
            team_key(row.team_name),
        )
        matches = matching_entries(candidates[key], row.classification, row.city)
        entry = matches[0] if len(matches) == 1 else None
        status = "matched" if entry else "ambiguous" if matches else "unmatched"
        values = {
            **asdict(row),
            "classification": {**row.classification, "gender": gender},
            "entry_id": entry.pk if entry else None,
            "link_status": status,
        }
        if apply:
            if entry:
                resolve_allocation_period(entry, affected)
            values["competition_class_id"] = allocation_class(
                season,
                values["classification"],
                classes,
                phase=entry.pool.phase if entry else "",
            )
        old = existing.get((row.row_number, row.column))
        changed += save_allocation(source, old, values, gender, apply=apply)
        decisions.append({
            "row": row.row_number,
            "column": row.column,
            "pool": row.pool_name,
            "team": row.team_name,
            "status": status,
            "entry": values["entry_id"],
        })
    for pool_id in sorted(affected):
        map_pool(affected[pool_id])
    return {
        "digest": digest,
        "published_on": published.isoformat(),
        "applied": apply,
        "rows": len(rows),
        "pools": len({(r.section, r.pool_name) for r in rows}),
        "sections": dict(Counter(r.section for r in rows)),
        "missing_age": sum(r.average_age is None for r in rows),
        "missing_points": sum(r.knkv_points is None for r in rows),
        "gender": gender,
        "links": dict(Counter(row["status"] for row in decisions)),
        "changed": changed,
        "decisions": decisions,
    }


def resolve_allocation_period(entry: PoolEntry, affected: dict[int, Pool]) -> None:
    """Resolve each linked whole-pool period once before aligning file classes."""
    if entry.pool_id not in affected:
        if not entry.pool.phase:
            resolve_pool_periods([entry.pool_id])
            entry.pool.refresh_from_db(
                fields=("phase", "competition_part", "phase_evidence")
            )
        affected[entry.pool_id] = entry.pool
    else:
        entry.pool = affected[entry.pool_id]


def save_allocation(
    source: AllocationSource | None,
    old: Allocation | None,
    values: dict[str, Any],
    gender: str,
    *,
    apply: bool,
) -> int:
    """Preserve snapshot evidence and reject changed identity links.

    Raises:
        ValueError: An existing snapshot would change identity or gender scope.

    """
    if old and old.classification.get("gender", "unknown") != gender:
        raise ValueError("Snapshot gender scope changed; preserve original provenance")
    if old and old.entry_id and old.entry_id != values["entry_id"]:
        raise ValueError("Allocation link changed; review identity before reimporting")
    if old is None:
        if apply:
            Allocation.objects.create(source=source, **values)
        return 1
    if any(getattr(old, field) != value for field, value in values.items()):
        if apply:
            Allocation.objects.filter(pk=old.pk).update(**values)
        return 1
    return 0


def pool_key(value: str) -> str:
    """Normalize verified code punctuation/padding; M/MW are the same midweek scope."""
    code = normalized(value)
    code = re.sub(r"^mwz(?=-?\d)", "mz", code)
    code = re.sub(r"^mw(?=-?\d)", "m", code)
    return re.sub(r"\d+", lambda match: f":{int(match.group())}:", code).replace(
        "-", ""
    )


def team_key(value: str) -> str:
    """Excel drops the leading quote in 't names; retain every other name token."""
    return re.sub(r"^[\u2019'](?=t\s)", "", normalized(value))


def matching_entries(
    candidates: list[PoolEntry], context: dict[str, str], city: str
) -> list[PoolEntry]:
    """Use a unique exact team/poule identity; town can resolve remaining duplicates."""
    matches = [
        entry
        for entry in candidates
        if classify(
            entry.pool.class_name, entry.pool.sport, season_edition(entry.pool.season)
        )[0].discipline
        == context["discipline"]
    ]
    if len(matches) > 1 and city:
        matches = [
            entry
            for entry in matches
            if normalized(entry.team.club.city) == normalized(city)
        ]
    return matches
