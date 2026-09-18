"""Explicit ORM read boundary for anonymous forecast exports and serving features."""

from collections import Counter
from datetime import datetime

from apps.competition.domain.score_forecast import MAX_DURATION
from apps.competition.models import Match


def features(source: Match) -> dict | None:
    """Use stable source IDs and exact official metadata, never infer age from names."""
    pool = source.pool
    if (
        pool is None
        or pool.mapping_status != "mapped"
        or pool.competition_class is None
    ):
        return None
    context = pool.competition_class
    edition = context.edition
    if edition.discipline not in {
        "indoor",
        "outdoor",
    } or context.playing_format not in {"four", "eight"}:
        return None
    if not source.playing_time_minutes or source.playing_time_minutes > MAX_DURATION:
        return None
    return {
        "match": str(source.pk),
        "season": str(edition.season_id),
        "discipline": edition.discipline,
        "phase": edition.phase,
        "gender": edition.gender,
        "category": context.category,
        "age_group": context.age_group,
        "colour": context.colour,
        "playing_format": context.playing_format,
        "team_kind": context.team_kind,
        "class": str(context.pk),
        "class_code": context.code,
        "pool": str(pool.pk),
        "pool_label": pool.name,
        "home": str(source.home_team_id),
        "away": str(source.away_team_id),
        "duration": source.playing_time_minutes,
        "duration_observed_at": source.playing_time_observed_at.isoformat()
        if source.playing_time_observed_at
        else None,
        "starts_at": source.starts_at.isoformat(),
    }


def export_rows(season: str, observed_through: datetime) -> dict:
    """Export all known revisions, with current metadata provenance disclosed."""
    query = (
        Match.objects
        .filter(season_id=season)
        .select_related("pool__competition_class__edition")
        .prefetch_related("revisions")
        .order_by("pk")
    )
    cups = set(query.filter(cup_fixture__isnull=False).values_list("pk", flat=True))
    excluded: Counter[str] = Counter()
    rows = []
    for source in query.iterator(chunk_size=1000):
        row = features(source)
        if source.pk in cups:
            excluded["cup"] += 1
            continue
        if row is None or source.home_team_id == source.away_team_id:
            excluded["unsupported_context_or_duration"] += 1
            continue
        revisions = [
            {
                "revision": revision.pk,
                "observed_at": revision.observed_at.isoformat(),
                "status": revision.status,
                "home_score": revision.home_score,
                "away_score": revision.away_score,
                "automatic_result": revision.automatic_result,
            }
            for revision in source.revisions.all()
            if revision.observed_at <= observed_through
        ]
        if (
            not revisions
            and source.result_observed_at
            and source.result_observed_at <= observed_through
        ):
            revisions = [
                {
                    "revision": 0,
                    "observed_at": source.result_observed_at.isoformat(),
                    "status": source.status,
                    "home_score": source.home_score,
                    "away_score": source.away_score,
                    "automatic_result": source.automatic_result,
                }
            ]
        row["revisions"] = revisions
        rows.append(row)
    return {
        "schema": 1,
        "exported_at": observed_through.isoformat(),
        "metadata_history": (
            "current snapshot; class, identity and schedule revisions are not available"
        ),
        "zero_score_policy": "quarantine unverified 0-0",
        "excluded": dict(excluded),
        "rows": rows,
    }
