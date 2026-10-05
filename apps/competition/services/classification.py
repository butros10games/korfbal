"""Map source pools into shared official classes without rewriting identities."""

from __future__ import annotations

from copy import copy
from dataclasses import asdict
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.competition.domain.classification import (
    SOURCE_GENDERS,
    VERSION,
    classify,
    ladder_context,
    validate,
)
from apps.competition.domain.competition_periods import outdoor_phase
from apps.competition.domain.source_context import context_value
from apps.competition.models import (
    Allocation,
    CompetitionClass,
    Match,
    Pool,
    PoolEntry,
    SeasonBinding,
    Team,
    TeamRating,
)
from apps.competition.services.class_alignment import (
    class_context,
    realign_pool_allocations,
    resolve_class,
)
from apps.competition.services.seasons import target_season
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_edition


MAX_CLASS_BATCH = 5000


def period_context(pool: Pool) -> tuple[dict[str, str], dict[str, Any]]:
    """Use a decided poule, or a self-routed season with whole-poule agreement."""
    if pool.phase:
        return {"phase": pool.phase}, {"source": "pool", "value": pool.phase}
    phase = pool.season.phase
    if phase not in {"autumn", "spring", "indoor", "full_season"}:
        return {}, {"reason": "season_phase_missing"}
    if (
        SeasonBinding.objects
        .filter(scope=pool.season)
        .exclude(season=pool.season)
        .exists()
    ):
        return {}, {"reason": "competing_binding"}
    if pool.sport in {"KORFBALL-ZA-WK", "KORFBALL-ZA-BK"}:
        consistent = phase == "indoor"
    elif pool.sport in {"KORFBALL-VE-WK", "KORFBALL-VE-BK"}:
        edition = season_edition(pool.season)
        days = [
            timezone.localdate(stamp)
            for stamp in Match.objects.filter(pool=pool).values_list(
                "starts_at", flat=True
            )
        ]
        consistent = edition is not None and outdoor_phase(days, edition).phase == phase
    else:
        consistent = False
    if not consistent:
        return {}, {"reason": "season_phase_unverified", "value": phase}
    return {"phase": phase}, {"source": "season", "value": phase}


def pool_gender_evidence(pool: Pool) -> tuple[dict[str, str], dict[str, Any]]:
    """Require every standing and fixture participant to agree on calibrated gender."""
    teams = set(PoolEntry.objects.filter(pool=pool).values_list("team_id", flat=True))
    for home, away in Match.objects.filter(pool=pool).values_list(
        "home_team_id", "away_team_id"
    ):
        teams.update((home, away))
    observed = [
        context_value(row.source_context, "Gender")
        for row in Team.objects.filter(pk__in=teams).only("source_context")
    ]
    raw = sorted({value for value in observed if isinstance(value, str) and value})
    evidence: dict[str, Any] = {"teams": len(teams), "values": raw}
    mapped = {SOURCE_GENDERS.get(value) for value in raw}
    if not observed or any(
        not isinstance(value, str) or not value for value in observed
    ):
        evidence["reason"] = "source_gender_missing"
    elif len(raw) != 1:
        evidence["reason"] = "source_gender_mixed"
    elif None in mapped:
        evidence["reason"] = "source_gender_unmapped"
    elif len(mapped) != 1:
        evidence["reason"] = "source_gender_mixed"
    else:
        evidence["reason"] = "source_gender_agreement"
        gender = mapped.pop()
        if isinstance(gender, str) and gender in {"mixed", "women"}:
            return {"gender": gender}, evidence
        evidence["reason"] = "source_gender_unmapped"
    return {}, evidence


def allocation_context(
    allocations: list[Allocation],
) -> tuple[dict[str, str], list[str]]:
    """Reconcile the latest worksheet context without overriding pool phase."""
    if not allocations:
        return {}, []
    latest = allocations[0].source.published_on
    current = [row for row in allocations if row.source.published_on == latest]
    context, issues = {}, []
    for field in set().union(*(row.classification for row in current)) - {"phase"}:
        values = {row.classification.get(field, "unknown") for row in current}
        if len(values) == 1:
            context[field] = values.pop()
        else:
            issues.append(f"allocation_conflicting_{field}")
    return context, issues


def plan_pool(pool: Pool, *, phase: str | None = None) -> dict[str, Any]:
    """Return a deterministic, serializable decision including its source evidence."""
    if phase is not None:
        pool = copy(pool)
        pool.phase = phase
    override = pool.mapping_override
    allocations = list(
        Allocation.objects
        .filter(entry__pool=pool)
        .select_related("source")
        .order_by("-source__published_on", "pk")
    )
    context, allocation_issues = allocation_context(allocations)
    # Rules follow the edition (July-June), not the playing season's start year:
    # a spring season starting in January belongs to the previous July's year.
    edition = season_edition(pool.season)
    period, period_evidence = period_context(pool)
    gender, gender_evidence = pool_gender_evidence(pool)
    original, _ = classify(
        pool.class_name, pool.sport, edition, context={**period, **gender}
    )
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
        edition,
        {**context, **override.get("values", {})},
        context={**period, **gender},
    )
    issues.extend(allocation_issues)
    evidence = {
        "class_name": pool.class_name,
        "pool_name": pool.name,
        "sport": pool.sport,
        "season_start": pool.season.start_date.isoformat(),
        "period": period_evidence,
        "gender": gender_evidence,
    }
    if allocations:
        evidence["allocations"] = sorted({
            row.source.digest
            for row in allocations
            if row.source.published_on == allocations[0].source.published_on
        })
    if override:
        reviewed = override.get("source")
        core = {"class_name", "pool_name", "sport", "season_start"}
        # Additive evidence does not invalidate a legacy review by itself;
        # current phase/gender contradictions are still checked above.
        if (
            not isinstance(reviewed, dict)
            or not core <= reviewed.keys()
            or any(evidence.get(key) != value for key, value in reviewed.items())
        ):
            issues.append("override_source_changed")
    status = (
        "conflict"
        if any(not issue.startswith("missing_") for issue in issues)
        else "partial"
        if issues
        else "mapped"
    )
    if (value.code == "unknown" and status != "conflict") or edition is None:
        status = "unresolved"
    return {
        "classification": asdict(value),
        **ladder_context(value, issues, edition),
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
        .select_for_update(of=("self",), no_key=True)
        .select_related("season")
        .get(pk=pool.pk)
    )
    decision = plan_pool(pool)
    class_id = None
    if decision["status"] not in {"unresolved", "conflict"}:
        native_season = target_season(pool.season, pool.sport, pool.phase)
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
        for key, value in changed.items():
            setattr(pool, key, value)
    allocations = realign_pool_allocations(pool) if class_id is not None else 0
    return {
        **decision,
        "changed": bool(changed) or bool(allocations),
        "allocations_realigned": allocations,
    }


def pool_classification(pool: Pool | None) -> dict[str, Any] | None:
    """Shared projection for source and native API views; no cached duplicate data."""
    if pool is None:
        return None
    context = None
    if pool.competition_class_id:
        row = pool.competition_class
        value = class_context(row)
        context = {
            **asdict(value),
            **ladder_context(value, pool.mapping_issues, season_edition(pool.season)),
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


def relevel_classes(
    *,
    apply: bool = False,
    limit: int = 200,
    after: int = 0,
    season: Season | None = None,
) -> dict[str, Any]:
    """Preview/apply a bounded batch preserving keys and orphaned rows.

    Raises:
        ValueError: The batch limit or cursor is invalid.

    """
    if not 0 < limit <= MAX_CLASS_BATCH or after < 0:
        raise ValueError("Class batch requires limit 1-5000 and a nonnegative cursor")
    query = (
        CompetitionClass.objects
        .filter(pk__gt=after)
        .select_related("edition__season")
        .order_by("pk")
    )
    if season is not None:
        seasons = {
            season.pk,
            *SeasonBinding.objects.filter(scope=season).values_list(
                "season_id", flat=True
            ),
        }
        query = query.filter(edition__season_id__in=seasons)
    rows = list(query[:limit])
    decisions = []
    changed_count = 0
    for row in rows:
        value = class_context(row)
        edition = season_edition(row.edition.season)
        metadata = ladder_context(value, validate(value, edition), edition)
        references = {
            "pools": Pool.objects.filter(competition_class=row).count(),
            "allocations": Allocation.objects.filter(competition_class=row).count(),
            "ratings": TeamRating.objects.filter(competition_class=row).count(),
        }
        changed = row.level != metadata["level"]
        changed_count += int(changed)
        if apply and changed:
            with transaction.atomic():
                locked = CompetitionClass.objects.select_for_update(no_key=True).get(
                    pk=row.pk
                )
                if locked.level != metadata["level"]:
                    CompetitionClass.objects.filter(pk=row.pk).update(
                        level=metadata["level"]
                    )
        decisions.append({
            "class": row.pk,
            "key": asdict(value),
            "edition": edition,
            "before": row.level,
            "after": metadata["level"],
            "changed": changed,
            **metadata,
            "references": references,
            "orphaned": not any(references.values()),
        })
    return {
        "applied": apply,
        "seen": len(rows),
        "changed": changed_count,
        "next_after": rows[-1].pk if len(rows) == limit else None,
        "decisions": decisions,
    }
