"""Explicit ORM read boundary for anonymous forecast exports and serving features."""

from collections import Counter
from datetime import datetime

from django.db.models import QuerySet

from apps.competition.domain.score_forecast import MAX_DURATION
from apps.competition.models import Match, Pool, Team


# Mixed and women's poules share labels; only KNKV allocation sheets disclose the
# gender. Such a poule remains its own context and never borrows a mixed baseline.
GENDER_PENDING = ["missing_gender"]


def supported_pool(pool: Pool | None) -> bool:
    """Accept fully mapped poules and those whose only unknown is the gender."""
    return (
        pool is not None
        and pool.competition_class is not None
        and (
            pool.mapping_status == "mapped"
            or (
                pool.mapping_status == "partial"
                and pool.mapping_issues == GENDER_PENDING
            )
        )
    )


def identity(team: Team) -> str | None:
    """Follow one club team across seasons through its native team."""
    group = team.group
    return str(group.local_team_id) if group and group.local_team_id else None


def features(source: Match) -> dict | None:
    """Use stable source IDs and exact official metadata, never infer age from names."""
    pool = source.pool
    if not supported_pool(pool):
        return None
    assert pool is not None
    context = pool.competition_class
    assert context is not None
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
        "home_identity": identity(source.home_team),
        "away_identity": identity(source.away_team),
        "duration": source.playing_time_minutes,
        "duration_observed_at": source.playing_time_observed_at.isoformat()
        if source.playing_time_observed_at
        else None,
        "starts_at": source.starts_at.isoformat(),
    }


def prior_features(source: Match) -> dict | None:
    """Describe an earlier-season result by context and cross-season team identity.

    Earlier editions predate playing-time imports and allocation sheets, so partial
    classifications remain usable; unknown fields stay explicit in the pace key.
    """
    pool = source.pool
    if (
        pool is None
        or pool.competition_class is None
        or pool.mapping_status not in {"mapped", "partial"}
    ):
        return None
    context = pool.competition_class
    edition = context.edition
    if edition.discipline not in {
        "indoor",
        "outdoor",
    } or context.playing_format not in {"four", "eight"}:
        return None
    home, away = identity(source.home_team), identity(source.away_team)
    if home is None or away is None or home == away:
        return None
    duration = source.playing_time_minutes
    return {
        "match": str(source.pk),
        "season": str(edition.season_id),
        "discipline": edition.discipline,
        "gender": edition.gender,
        "category": context.category,
        "age_group": context.age_group,
        "colour": context.colour,
        "playing_format": context.playing_format,
        "team_kind": context.team_kind,
        "class_code": context.code,
        "home_identity": home,
        "away_identity": away,
        "duration": duration if duration and duration <= MAX_DURATION else None,
        "starts_at": source.starts_at.isoformat(),
    }


def revisions(source: Match, observed_through: datetime) -> list[dict]:
    """Return every known score revision observed by the export cutoff."""
    rows = [
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
        not rows
        and source.result_observed_at
        and source.result_observed_at <= observed_through
    ):
        rows = [
            {
                "revision": 0,
                "observed_at": source.result_observed_at.isoformat(),
                "status": source.status,
                "home_score": source.home_score,
                "away_score": source.away_score,
                "automatic_result": source.automatic_result,
            }
        ]
    return rows


def season_matches(season: str) -> QuerySet[Match]:
    """Load each row's context and cross-season identities in one query."""
    return (
        Match.objects
        .filter(season_id=season)
        .select_related(
            "pool__competition_class__edition", "home_team__group", "away_team__group"
        )
        .prefetch_related("revisions")
        .order_by("pk")
    )


def export_rows(
    season: str, observed_through: datetime, prior_seasons: tuple[str, ...] = ()
) -> dict:
    """Export all known revisions, with current metadata provenance disclosed."""
    query = season_matches(season)
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
        row["revisions"] = revisions(source, observed_through)
        rows.append(row)
    report = {
        "schema": 2 if prior_seasons else 1,
        "season": season,
        "exported_at": observed_through.isoformat(),
        "metadata_history": (
            "current snapshot; class, identity and schedule revisions are not available"
        ),
        "zero_score_policy": "quarantine unverified 0-0",
        "excluded": dict(excluded),
        "rows": rows,
    }
    if prior_seasons:
        report.update(prior_export(prior_seasons, observed_through))
    return report


def prior_export(seasons: tuple[str, ...], observed_through: datetime) -> dict:
    """Export earlier source seasons' results as priors, never as test labels."""
    excluded: Counter[str] = Counter()
    rows = []
    for season in seasons:
        query = season_matches(season).filter(cup_fixture__isnull=True)
        for source in query.iterator(chunk_size=1000):
            row = prior_features(source)
            if row is None:
                excluded["unsupported_context_or_identity"] += 1
                continue
            row["revisions"] = revisions(source, observed_through)
            if row["revisions"]:
                rows.append(row)
    return {
        "prior_seasons": list(seasons),
        "prior_excluded": dict(excluded),
        "prior_rows": rows,
    }
