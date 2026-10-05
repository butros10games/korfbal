"""Share immutable class identities and realign linked allocation evidence."""

from dataclasses import asdict
from typing import Any

from apps.competition.domain.classification import (
    Classification,
    classify,
    level,
    validate,
)
from apps.competition.models import (
    Allocation,
    CompetitionClass,
    CompetitionEdition,
    Pool,
)
from apps.competition.services.seasons import INDOOR, OUTDOOR, target_season
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_edition


CLASS_FIELDS = (
    "code",
    "category",
    "age_group",
    "team_kind",
    "colour",
    "playing_format",
)


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
    value = Classification(**values)
    expected = level(
        value, validate(value, season_edition(season)), season_edition(season)
    )
    if row.level != expected:
        CompetitionClass.objects.filter(pk=row.pk, level=row.level).update(
            level=expected
        )
        row.level = expected
    return row


def class_context(row: CompetitionClass) -> Classification:
    """Read an immutable class key, independently of any pool using it."""
    return Classification(
        **{field: getattr(row, field) for field in CLASS_FIELDS},
        **{
            field: getattr(row.edition, field)
            for field in ("discipline", "phase", "gender")
        },
    )


def allocation_class(
    season: Season, values: dict[str, str], classes: dict, *, phase: str = ""
) -> int:
    """Resolve each distinct source context once per file.

    ``phase`` is the linked poule's competition period: its class lives in the
    native season that period publishes into, like the poule's own class.

    Raises:
        ValueError: A source context contradicts the season-specific rules.

    """
    key = (*sorted(values.items()), ("phase", phase))
    if key not in classes:
        # Worksheet defaults describe its publication, not a linked pool's period.
        actual = {**values, "phase": phase or "unknown"}
        context, issues = classify("", "", season_edition(season), actual)
        if any(not issue.startswith("missing_") for issue in issues):
            raise ValueError(f"Conflicting allocation classification: {issues}")
        native_season = target_season(
            season,
            {"indoor": INDOOR, "outdoor": OUTDOOR}.get(context.discipline, ""),
            phase,
        )
        if native_season is None:
            raise ValueError("Allocation discipline has no native season mapping")
        classes[key] = resolve_class(
            native_season,
            {
                "classification": asdict(context),
                "level": level(context, issues, season_edition(season)),
            },
        ).pk
    return classes[key]


def realign_pool_allocations(pool: Pool) -> int:
    """Move a poule's linked allocations to the class of its resolved period.

    Returns:
        The number of allocations whose class changed.

    """
    changed = 0
    classes: dict = {}
    for allocation in Allocation.objects.filter(entry__pool=pool).select_related(
        "source__season"
    ):
        try:
            class_id = allocation_class(
                allocation.source.season,
                allocation.classification,
                classes,
                phase=pool.phase,
            )
        except ValueError:
            continue
        if allocation.competition_class_id != class_id:
            Allocation.objects.filter(pk=allocation.pk).update(
                competition_class_id=class_id
            )
            changed += 1
    return changed
