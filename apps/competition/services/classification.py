"""Map source pools into shared official classes without rewriting identities."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from django.db import transaction

from apps.competition.domain.classification import VERSION, classify, level
from apps.competition.models import (
    Allocation,
    CompetitionClass,
    CompetitionEdition,
    Pool,
)
from apps.competition.services.seasons import target_season
from apps.schedule.models import Season


CLASS_FIELDS = (
    "code",
    "category",
    "age_group",
    "team_kind",
    "colour",
    "playing_format",
)


def plan_pool(pool: Pool) -> dict[str, Any]:
    """Return a deterministic, serializable decision including its source evidence."""
    override = pool.mapping_override
    allocations = list(
        Allocation.objects
        .filter(entry__pool=pool)
        .select_related("source")
        .order_by("-source__published_on", "pk")
    )
    context = {}
    allocation_issues = []
    if allocations:
        latest = allocations[0].source.published_on
        current = [row for row in allocations if row.source.published_on == latest]
        for field in set().union(*(row.classification for row in current)):
            values = {row.classification.get(field, "unknown") for row in current}
            if len(values) == 1:
                context[field] = values.pop()
            else:
                allocation_issues.append(f"allocation_conflicting_{field}")
    original, _ = classify(pool.class_name, pool.sport, pool.season.start_date.year)
    reviewed_fields = override.get("values", {})
    for field, inferred in asdict(original).items():
        if (
            field not in reviewed_fields
            and inferred != "unknown"
            and context.get(field, "unknown") not in {"unknown", inferred}
        ):
            allocation_issues.append(f"allocation_source_conflicting_{field}")
    value, issues = classify(
        pool.class_name,
        pool.sport,
        pool.season.start_date.year,
        {**context, **override.get("values", {})},
    )
    issues.extend(allocation_issues)
    evidence = {
        "class_name": pool.class_name,
        "pool_name": pool.name,
        "sport": pool.sport,
        "season_start": pool.season.start_date.isoformat(),
    }
    if allocations:
        evidence["allocations"] = sorted({
            row.source.digest
            for row in allocations
            if row.source.published_on == allocations[0].source.published_on
        })
    if override and override.get("source") != evidence:
        issues.append("override_source_changed")
    status = (
        "conflict"
        if any(not issue.startswith("missing_") for issue in issues)
        else "partial"
        if issues
        else "mapped"
    )
    if value.code == "unknown" and status != "conflict":
        status = "unresolved"
    return {
        "classification": asdict(value),
        "level": level(value, issues),
        "status": status,
        "issues": issues,
        "evidence": evidence,
        "version": VERSION,
    }


@transaction.atomic
def map_pool(pool: Pool) -> dict[str, Any]:
    """Persist a decision under a source lock; unchanged reruns issue no writes."""
    pool = (
        Pool.objects
        .select_for_update(of=("self",))
        .select_related("season")
        .get(pk=pool.pk)
    )
    decision = plan_pool(pool)
    class_id = None
    if decision["status"] not in {"unresolved", "conflict"}:
        native_season = target_season(pool.season, pool.sport)
        if native_season is not None:
            class_id = resolve_class(native_season, decision).pk
    updates = {
        "competition_class_id": class_id,
        "mapping_status": decision["status"],
        "mapping_issues": decision["issues"],
        "mapping_version": VERSION,
        "mapping_evidence": decision["evidence"],
    }
    changed = {
        key: value for key, value in updates.items() if getattr(pool, key) != value
    }
    if changed:
        Pool.objects.filter(pk=pool.pk).update(**changed)
    return {**decision, "changed": bool(changed)}


def pool_classification(pool: Pool | None) -> dict[str, Any] | None:
    """Shared projection for source and native API views; no cached duplicate data."""
    if pool is None:
        return None
    context = None
    if pool.competition_class_id:
        row = pool.competition_class
        edition = row.edition
        context = {
            **{field: getattr(row, field) for field in CLASS_FIELDS},
            "level": row.level,
            "discipline": edition.discipline,
            "phase": edition.phase,
            "gender": edition.gender,
        }
    return {
        "status": pool.mapping_status,
        "issues": pool.mapping_issues,
        "version": pool.mapping_version,
        "context": context,
        "source_label": pool.class_name,
        "poule": pool.name,
        "reviewed": bool(pool.mapping_override),
    }


def resolve_class(season: Season, decision: dict[str, Any]) -> CompetitionClass:
    """Share classes between spreadsheet allocations and linked provider pools."""
    values = decision["classification"]
    edition, _ = CompetitionEdition.objects.get_or_create(
        season=season,
        **{field: values[field] for field in ("discipline", "phase", "gender")},
    )
    row, _ = CompetitionClass.objects.get_or_create(
        edition=edition,
        **{field: values[field] for field in CLASS_FIELDS},
        defaults={"level": decision["level"]},
    )
    return row
