"""Batch the existing legacy predictor without changing its historical semantics."""

from collections.abc import Callable
from datetime import datetime
from functools import lru_cache, partial

from apps.competition.models import Allocation, Match, RatingConfiguration
from apps.competition.services.match_prediction import (
    known_results,
    predict_allocations,
    unavailable,
)
from apps.competition.services.rating_preview import rate_class


def unavailable_reason(
    source: Match, configuration: RatingConfiguration | None, cutoff: datetime
) -> str:
    """Preserve the single-match predictor's fallback boundaries."""
    native = source.local_match
    assert native is not None
    if configuration is None:
        return "no_configuration"
    if cutoff < configuration.effective_at:
        return "before_baseline"
    if source.season_id != native.season_id or source.pool is None:
        return "no_allocation_match"
    if (
        source.pool.mapping_status != "mapped"
        or source.pool.competition_class_id is None
    ):
        return "unresolved_competition"
    return ""


def replay_class(
    source: Match,
    configuration: RatingConfiguration,
    cutoff: datetime,
    cache: dict,
    eligible: list[Allocation],
) -> dict:
    """Reuse one replay while retaining target exclusion for divergent schedules."""
    assert source.pool is not None
    class_id = source.pool.competition_class_id
    assert class_id is not None
    key = (
        configuration.pk,
        class_id,
        cutoff,
        source.pk if source.starts_at < cutoff else None,
    )
    if key not in cache:
        cache.clear()
        history = known_results(source, configuration.effective_at, cutoff)
        rows, _ = rate_class(
            class_id, eligible, history, configuration.b_scale, configuration.b_k_factor
        )
        cache[key] = {row["team"]: row for row in rows}
    return cache[key]


def predictions(
    identities: list[str], through: datetime, progress: Callable[[str], None]
) -> dict:
    """Reuse class allocations and same-kickoff replay within this export only."""
    configurations = {}
    sources = (
        Match.objects
        .filter(pk__in=identities)
        .select_related("local_match__season", "pool", "home_team", "away_team")
        .order_by("pool__competition_class_id", "starts_at", "pk")
    )

    @lru_cache(maxsize=64)
    def members(configuration_id: int, class_id: int) -> list[Allocation]:
        configuration = configurations[configuration_id]
        return list(
            Allocation.objects.filter(
                source_id__in=configuration.source_ids,
                source__season_id=configuration.season_id,
                competition_class_id=class_id,
                competition_class__edition__season_id=configuration.season_id,
            ).select_related(
                "entry__team__group", "entry__pool", "competition_class__edition"
            )
        )

    by_season = {}
    replay_cache: dict = {}
    result = {}
    for index, source in enumerate(sources.iterator(chunk_size=250), start=1):
        native = source.local_match
        if native is None:
            result[str(source.pk)] = unavailable("not_published")
            continue
        if native.season_id not in by_season:
            configuration = RatingConfiguration.objects.filter(
                season_id=native.season_id, active=True
            ).first()
            by_season[native.season_id] = configuration
            if configuration:
                configurations[configuration.pk] = configuration
        configuration = by_season[native.season_id]
        cutoff = min(through, native.start_time)
        reason = unavailable_reason(source, configuration, cutoff)
        if reason:
            prediction = unavailable(reason)
        else:
            assert source.pool is not None
            assert configuration is not None
            class_id = source.pool.competition_class_id
            assert class_id is not None
            prediction = predict_allocations(
                native,
                source,
                configuration,
                cutoff,
                batch=(
                    members(configuration.pk, class_id),
                    partial(replay_class, source, configuration, cutoff, replay_cache),
                ),
            )
        result[str(source.pk)] = prediction
        if index % 250 == 0:
            progress(f"Legacy comparisons {index}/{len(identities)}")
    return result
