"""Aggregate-only database evidence for one existing historical edition."""

from django.db.models import Count, F, IntegerField, Sum
from django.db.models.functions import Cast, ExtractMonth

from apps.competition.models import HistoricalResource, Match
from apps.schedule.models import Season


SKIP_REASONS = (
    "not_played",
    "unsupported_sport",
    "outside_season_dates",
    "sport_mismatch",
    "invalid_timestamp",
    "not_historical",
    "app_has_poule",
    "club_unknown",
    "incomplete",
    "poule_unknown",
    "not_persisted",
    "self_match",
)


def source_coverage_preview(edition: int) -> dict[str, object]:
    """Read counts and bounds without HTTP, checkpoint changes or content writes.

    Empty team probes and null scan bounds describe tested discovery only; no
    count of final stored fixtures certifies all possible source IDs or months.
    """
    seasons = Season.objects.filter(edition=edition)
    resources = HistoricalResource.objects.filter(season__in=seasons)
    sites = resources.filter(provider__in=("korfbalnl", "uitslagen"))
    skipped = sites.aggregate(**{
        reason: Sum(Cast(f"evidence__skipped__{reason}", IntegerField()))
        for reason in SKIP_REASONS
    })
    return {
        "edition": edition,
        "http_requests": 0,
        "domain_writes": 0,
        "operational_writes": [],
        "seasons": list(
            seasons.order_by("start_date").values(
                "phase",
                "discipline",
                "data_coverage",
                "coverage_reason",
                "data_unavailable",
            )
        ),
        "checkpoints": list(
            resources
            .values(
                "provider",
                "kind",
                "state",
                "coverage",
                "reason",
            )
            .annotate(count=Count("pk"))
            .order_by("provider", "kind", "state", "coverage", "reason")
        ),
        "result_months": list(
            Match.objects
            .filter(season__in=seasons)
            .annotate(month=ExtractMonth("starts_at"), sport=F("home_team__sport"))
            .values("season__phase", "sport", "month", "status")
            .annotate(count=Count("pk"))
            .order_by("season__phase", "sport", "month", "status")
        ),
        "scan_ranges": list(
            resources
            .filter(provider="app", kind="edition_scan")
            .values("evidence__low", "evidence__high")
            .distinct()
        ),
        "site_skip_reasons": {
            reason: count for reason, count in skipped.items() if count
        },
        "site_empty_with_returned_rows": sites.filter(
            coverage="empty",
            evidence__rows__gt=0,
        ).count(),
        "app_team_empty_probes": resources.filter(
            provider="app",
            kind="edition_team",
            coverage="empty",
            reason="no_season_data",
        ).count(),
        "app_pool_checkpoints": resources.filter(
            provider="app",
            kind__in=("pool", "edition_pool"),
        ).count(),
        "coverage_certified_complete": False,
        "scope": (
            "Stored observations for this edition; untested IDs and source "
            "endpoints remain unknown."
        ),
    }
