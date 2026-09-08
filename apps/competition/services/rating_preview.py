"""Read-only, allocation-scoped experiments; never replace published ratings."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any

from apps.competition.domain.elo import INITIAL_RATING, RatedResult, calculate
from apps.competition.models import Allocation, AllocationSource, Match
from apps.schedule.models import Season


MODEL = "allocation-elo-preview-v1"


@dataclass(frozen=True)
class PreviewParameters:
    """Explicit experimental parameters and the intended replay window."""

    effective_at: datetime
    through: datetime
    scale: float
    k_factor: float


def preview_ratings(
    season: Season,
    source_ids: list[int],
    parameters: PreviewParameters,
) -> dict[str, Any]:
    """Replay only fixtures in the exact allocated pool, keeping source points intact.

    The caller explicitly chooses a pre-competition effective date independently
    of the file publication date. B parameters use original KNKV point units.
    A uses neutral 1500/400/24 within each official class, never a class offset.

    Raises:
        ValueError: The window or source selection is invalid or ambiguous.

    """
    effective_at, through = parameters.effective_at, parameters.through
    scale, k_factor = parameters.scale, parameters.k_factor
    calculate({}, [], scale=scale, k_factor=k_factor)
    if (
        effective_at.tzinfo is None
        or through.tzinfo is None
        or through < effective_at
        or not season.start_date <= effective_at.date() <= season.end_date
    ):
        raise ValueError("Require an aware, ordered window starting within the season")
    sources = list(
        AllocationSource.objects.filter(season=season, pk__in=source_ids).order_by("pk")
    )
    if not sources or len(sources) != len(set(source_ids)):
        raise ValueError("Select existing allocation sources in the selected season")
    allocations = list(
        Allocation.objects
        .filter(source__in=sources)
        .select_related(
            "source", "entry__team", "entry__pool", "competition_class__edition"
        )
        .order_by("pk")
    )
    seen: set[tuple[int, int]] = set()
    groups: dict[int, list[Allocation]] = defaultdict(list)
    excluded = Counter()
    excluded_rows = []
    for allocation in allocations:
        reason = exclusion(allocation)
        if reason:
            excluded[reason] += 1
            excluded_rows.append({
                "allocation": allocation.pk,
                "team": allocation.team_name,
                "reason": reason,
            })
            continue
        entry = allocation.entry
        assert entry is not None
        assert allocation.competition_class_id is not None
        key = (allocation.competition_class_id, entry.team_id)
        if key in seen:
            raise ValueError(
                "Multiple baselines for one team/class; select one snapshot"
            )
        seen.add(key)
        groups[allocation.competition_class_id].append(allocation)
    results = list(
        Match.objects.filter(
            season=season,
            starts_at__gte=effective_at,
            starts_at__lte=through,
            status="FINAL",
            automatic_result=False,
            home_score__isnull=False,
            away_score__isnull=False,
            result_observed_at__isnull=False,
        ).order_by("starts_at", "external_id")
    )
    rows = []
    used: set[int] = set()
    for class_id, members in sorted(groups.items()):
        class_rows, match_ids = rate_class(class_id, members, results, scale, k_factor)
        rows.extend(class_rows)
        used.update(match_ids)
    return {
        "model": MODEL,
        "applied": False,
        "season": str(season.pk),
        "effective_at": effective_at.isoformat(),
        "through": through.isoformat(),
        "b_parameters": {
            "scale": scale,
            "k_factor": k_factor,
            "units": "KNKV points",
            "calibrated": False,
        },
        "a_policy": (
            "Neutral 1500 within official class; "
            "class level is ordinal, not an Elo offset"
        ),
        "sources": [
            {
                "id": source.pk,
                "digest": source.digest,
                "label": source.label,
                "published_on": source.published_on.isoformat(),
            }
            for source in sources
        ],
        "baseline_assumption": (
            "Selected files represent strength before effective_at; "
            "publication date is not proof of baseline timing"
        ),
        "eligible_results": len(results),
        "used_results": len(used),
        "unused_results": len(results) - len(used),
        "unused_result_ids": [
            match.external_id for match in results if match.pk not in used
        ],
        "result_fingerprint": sha256(
            json.dumps([
                (
                    match.external_id,
                    match.starts_at.isoformat(),
                    match.pool_id,
                    match.home_team_id,
                    match.away_team_id,
                    match.home_score,
                    match.away_score,
                )
                for match in results
            ]).encode()
        ).hexdigest(),
        "excluded": dict(excluded),
        "excluded_allocations": excluded_rows,
        "results": rows,
    }


def exclusion(allocation: Allocation) -> str:
    """Require exact linked and reviewed classification; preserve missing baselines."""
    entry = allocation.entry
    context = allocation.competition_class
    if entry is None or allocation.link_status != "matched":
        return "unlinked"
    if (
        context is None
        or entry.pool.mapping_status != "mapped"
        or entry.pool.competition_class_id != context.pk
    ):
        return "unresolved_context"
    if context.category not in {"a", "b", "top"}:
        return "unsupported_category"
    if context.category == "b" and allocation.knkv_points is None:
        return "missing_baseline"
    return ""


def rate_class(
    class_id: int,
    members: list[Allocation],
    matches: list[Match],
    scale: float,
    k_factor: float,
) -> tuple[list[dict[str, Any]], set[int]]:
    """Isolate editions, gender, age/colour, formats and classes before replay."""
    context = members[0].competition_class
    assert context is not None
    entries = {row.entry.team_id: row for row in members if row.entry is not None}
    seeds = {
        team: float(row.knkv_points)
        if row.knkv_points is not None and context.category == "b"
        else INITIAL_RATING
        for team, row in entries.items()
    }
    results = []
    used = set()
    for match in matches:
        home, away = entries.get(match.home_team_id), entries.get(match.away_team_id)
        if home is None or away is None or home.entry is None or away.entry is None:
            continue
        if (
            match.home_team_id == match.away_team_id
            or match.pool_id != home.entry.pool_id
            or match.pool_id != away.entry.pool_id
        ):
            continue
        assert match.home_score is not None
        assert match.away_score is not None
        results.append(
            RatedResult(
                match.external_id,
                match.starts_at,
                match.home_team_id,
                match.away_team_id,
                match.home_score,
                match.away_score,
            )
        )
        used.add(match.pk)
    parameters = (
        {"scale": scale, "k_factor": k_factor} if context.category == "b" else {}
    )
    ratings = calculate(
        {team: str(class_id) for team in entries}, results, initial=seeds, **parameters
    )
    rows = []
    for team, allocation in entries.items():
        rating = ratings[team]
        entry = allocation.entry
        assert entry is not None
        rows.append({
            "allocation": allocation.pk,
            "team": entry.team.external_id,
            "name": entry.team.name,
            "club": entry.team.club_id,
            "class": class_id,
            "class_code": context.code,
            "class_level": context.level,
            "category": context.category,
            "discipline": context.edition.discipline,
            "phase": context.edition.phase,
            "gender": context.edition.gender,
            "age_group": context.age_group,
            "team_kind": context.team_kind,
            "playing_format": context.playing_format,
            "colour": context.colour,
            "average_age": str(allocation.average_age)
            if allocation.average_age is not None
            else None,
            "original_knkv_points": str(allocation.knkv_points)
            if allocation.knkv_points is not None
            else None,
            "baseline": seeds[team],
            "rating": round(rating.value, 4),
            "change": round(rating.value - seeds[team], 4),
            "games": rating.games,
            "provisional": True,
            "comparison_group": f"{class_id}:{rating.comparison_group}",
        })
    return rows, used
