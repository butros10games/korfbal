"""Live allocation Elo selected explicitly per season, with corrected-result replay."""

from datetime import datetime
from hashlib import sha256
import json
from operator import itemgetter
from typing import Any

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.competition.models import Allocation, Match, RatingConfiguration, Team
from apps.competition.services.rating_preview import PreviewParameters, preview_ratings
from apps.schedule.models import Season


MODEL = "knkv-seeded-elo-v1"
CACHE_SECONDS = 300


def published_ratings(configuration: RatingConfiguration) -> dict[str, Any]:
    """Return only the selected rating population; never mix default Elo into it."""
    now = timezone.now()
    teams = list(
        Team.objects.filter(season=configuration.season).values(
            "id",
            "external_id",
            "name",
            "sport",
            "club_id",
        )
    )
    key = fingerprint(configuration, now, teams)
    cached = cache.get(key)
    if cached is not None:
        return cached
    report = preview_ratings(
        configuration.season,
        configuration.source_ids,
        PreviewParameters(
            configuration.effective_at,
            now,
            configuration.b_scale,
            configuration.b_k_factor,
        ),
    )
    by_external = {team["external_id"]: team for team in teams}
    rows = [{**row, **by_external[row["team"]]} for row in report["results"]]
    response = {
        "model": MODEL,
        "computed_at": now.isoformat(),
        "results": sorted(
            rows,
            key=lambda row: (
                row["comparison_group"],
                -row["rating"],
                row["external_id"],
            ),
        ),
        "metadata": {
            "effective_at": report["effective_at"],
            "through": report["through"],
            "sources": report["sources"],
            "b_parameters": report["b_parameters"],
            "a_policy": report["a_policy"],
            "baseline_assumption": report["baseline_assumption"],
            "excluded": report["excluded"],
            "used_results": report["used_results"],
            "unused_results": report["unused_results"],
        },
    }
    cache.set(key, response, CACHE_SECONDS)
    return response


def fingerprint(
    configuration: RatingConfiguration, now: datetime, teams: list[dict]
) -> str:
    """Invalidate on source, mapping, population, result or configuration changes.

    Content fingerprints also see direct ORM corrections and newly elapsed final
    fixtures. Poll freshness alone is deliberately excluded from match inputs.
    """
    allocations = (
        Allocation.objects
        .filter(source_id__in=configuration.source_ids)
        .order_by("pk")
        .values(
            "id",
            "source_id",
            "source__digest",
            "source__label",
            "source__published_on",
            "entry_id",
            "entry__team_id",
            "entry__pool_id",
            "link_status",
            "team_name",
            "knkv_points",
            "average_age",
            "competition_class_id",
            "entry__pool__mapping_status",
            "entry__pool__competition_class_id",
            "competition_class__code",
            "competition_class__level",
            "competition_class__category",
            "competition_class__age_group",
            "competition_class__colour",
            "competition_class__team_kind",
            "competition_class__playing_format",
            "competition_class__edition__discipline",
            "competition_class__edition__phase",
            "competition_class__edition__gender",
        )
    )
    matches = (
        Match.objects
        .filter(
            season=configuration.season,
            starts_at__gte=configuration.effective_at,
            starts_at__lte=now,
            status="FINAL",
            automatic_result=False,
            home_score__isnull=False,
            away_score__isnull=False,
            result_observed_at__isnull=False,
        )
        .order_by("pk")
        .values_list(
            "external_id",
            "pool_id",
            "home_team_id",
            "away_team_id",
            "starts_at",
            "home_score",
            "away_score",
        )
    )
    payload = [
        configuration.source_ids,
        configuration.effective_at,
        configuration.b_scale,
        configuration.b_k_factor,
        list(allocations),
        list(matches),
        sorted(teams, key=itemgetter("id")),
    ]
    digest = sha256(
        json.dumps(payload, default=str, sort_keys=True).encode()
    ).hexdigest()
    return f"competition:{MODEL}:{configuration.season_id}:{digest}"


@transaction.atomic
def configure_ratings(
    season: Season,
    source_ids: list[int],
    parameters: PreviewParameters,
    *,
    apply: bool,
) -> dict[str, Any]:
    """Validate before activation and serialize concurrent changes by season.

    Raises:
        ValueError: No usable baselines exist or preview validation fails.

    """
    if apply:
        Season.objects.select_for_update().get(pk=season.pk)
    report = preview_ratings(season, source_ids, parameters)
    if not report["results"]:
        raise ValueError("No usable allocation baselines; ratings were not activated")
    values = {
        "source_ids": sorted(set(source_ids)),
        "effective_at": parameters.effective_at,
        "b_scale": parameters.scale,
        "b_k_factor": parameters.k_factor,
        "active": True,
    }
    existing = RatingConfiguration.objects.filter(season=season).first()
    changed = existing is None or any(
        getattr(existing, key) != value for key, value in values.items()
    )
    if apply and changed:
        RatingConfiguration.objects.update_or_create(season=season, defaults=values)
    return {"applied": apply, "changed": changed, "model": MODEL, "preview": report}


@transaction.atomic
def disable_ratings(season: Season, *, apply: bool) -> dict[str, Any]:
    """Restore the legacy model without deleting selected baselines or history."""
    if apply:
        Season.objects.select_for_update().get(pk=season.pk)
    query = RatingConfiguration.objects.filter(season=season, active=True)
    changed = query.exists()
    if apply:
        query.update(active=False, updated_at=timezone.now())
    return {"applied": apply, "changed": changed, "model": "elo-v1"}
