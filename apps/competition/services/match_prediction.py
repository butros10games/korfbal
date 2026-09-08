"""Match-specific starting strength using only results known before kickoff."""

from datetime import datetime
from typing import Any

from django.db.models import FETCH_RAISE, OuterRef, Subquery
from django.utils import timezone

from apps.competition.domain.elo import RATING_SCALE
from apps.competition.models import (
    Allocation,
    Match,
    RatingConfiguration,
    ResultRevision,
)
from apps.competition.services.context_prediction import context_prediction
from apps.competition.services.rating_preview import exclusion, rate_class
from apps.schedule.models import Match as NativeMatch


def match_prediction(match: NativeMatch) -> dict[str, Any]:
    """Resolve exact native/provider identities without comparing unrelated classes."""
    configuration = RatingConfiguration.objects.filter(
        season_id=match.season_id, active=True
    ).first()
    if configuration is None:
        return unavailable("no_configuration")
    cutoff = min(timezone.now(), match.start_time)
    if cutoff < configuration.effective_at:
        return unavailable("before_baseline")
    source = (
        Match.objects
        .filter(local_match=match, season_id=match.season_id)
        .select_related("pool", "home_team", "away_team")
        .fetch_mode(FETCH_RAISE)
        .first()
    )
    if source is None or source.pool is None:
        return unavailable("no_allocation_match")
    if (
        source.pool.mapping_status != "mapped"
        or source.pool.competition_class_id is None
    ):
        return unavailable("unresolved_competition")
    return predict_allocations(match, source, configuration, cutoff)


def predict_allocations(
    match: NativeMatch,
    source: Match,
    configuration: RatingConfiguration,
    cutoff: datetime,
) -> dict[str, Any]:
    """Replay the exact class containing both allocated match opponents."""
    assert source.pool is not None
    assert source.pool.competition_class_id is not None
    members = list(
        Allocation.objects.filter(
            source_id__in=configuration.source_ids,
            source__season_id=match.season_id,
            competition_class_id=source.pool.competition_class_id,
            competition_class__edition__season_id=match.season_id,
        ).select_related(
            "entry__team__group", "entry__pool", "competition_class__edition"
        )
    )
    eligible = [row for row in members if not exclusion(row)]
    identities = [
        (row.entry.team_id, row.entry.pool_id) for row in eligible if row.entry
    ]
    if len({team for team, _ in identities}) != len(identities):
        return unavailable("ambiguous_baseline")
    home = next(
        (
            row
            for row in eligible
            if row.entry
            and row.entry.team_id == source.home_team_id
            and row.entry.pool_id == source.pool_id
        ),
        None,
    )
    away = next(
        (
            row
            for row in eligible
            if row.entry
            and row.entry.team_id == source.away_team_id
            and row.entry.pool_id == source.pool_id
        ),
        None,
    )
    if home is None or away is None:
        return unavailable("missing_baseline")
    # Provider home/away orientation must agree with the native match being shown.
    if not native_sides_match(match, home, away):
        return unavailable("identity_conflict")
    history = known_results(source, configuration.effective_at, cutoff)
    rows, _ = rate_class(
        source.pool.competition_class_id,
        eligible,
        history,
        configuration.b_scale,
        configuration.b_k_factor,
    )
    ratings = {row["team"]: row for row in rows}
    home_rating, away_rating = (
        ratings[source.home_team.external_id],
        ratings[source.away_team.external_id],
    )
    context = home.competition_class
    assert context is not None
    scale = configuration.b_scale if context.category == "b" else RATING_SCALE
    expected = 1 / (
        1
        + 10
        ** max(-100, min(100, (away_rating["rating"] - home_rating["rating"]) / scale))
    )
    calibration = context_prediction(
        context=(
            f"{context.edition.discipline}|{context.category}|{context.code}|"
            f"{context.age_group}|{context.colour}|{context.playing_format}"
        ),
        # The frozen model was fitted on preseason KNKV points, not Elo replay.
        ratings=(home_rating["baseline"], away_rating["baseline"]),
        as_of=cutoff,
        season=match.season.name if match.season else "",
        rating_scale=scale,
    )
    return {
        **({"outcome_calibration": calibration} if calibration else {}),
        "status": "seeded",
        "model": "knkv-prematch-v1",
        "as_of": cutoff.isoformat(),
        "home_expected_result": expected,
        "home_rating": home_rating["rating"],
        "away_rating": away_rating["rating"],
        "home_games": home_rating["games"],
        "away_games": away_rating["games"],
        "discipline": context.edition.discipline,
        "category": context.category,
        "class_code": context.code,
        "provisional": True,
    }


def native_sides_match(match: NativeMatch, home: Allocation, away: Allocation) -> bool:
    """Do not silently reverse a provider rating or use a different native team."""
    for allocation, team_id in ((home, match.home_team_id), (away, match.away_team_id)):
        entry = allocation.entry
        if (
            entry is None
            or entry.team.group is None
            or entry.team.group.local_team_id != team_id
        ):
            return False
    return True


def known_results(source: Match, effective: datetime, cutoff: datetime) -> list[Match]:
    """Reconstruct the latest observed revision at the prediction's cutoff.

    Exclude the target and simultaneous/future starts. Later score corrections
    cannot leak backwards; unchanged polling does not erase an earlier revision.
    If revision history is unavailable, only a current result observed before
    cutoff can be used. Late imports are never treated as previously known scores.
    """
    assert source.pool is not None
    revisions = ResultRevision.objects.filter(
        match_id=OuterRef("pk"), observed_at__lt=cutoff
    ).order_by("-observed_at", "-pk")
    query = (
        Match.objects
        .filter(
            season_id=source.season_id,
            pool__competition_class_id=source.pool.competition_class_id,
            starts_at__gte=effective,
            starts_at__lt=cutoff,
        )
        .exclude(pk=source.pk)
        .annotate(**{
            f"known_{field}": Subquery(revisions.values(field)[:1])
            for field in (
                "id",
                "status",
                "home_score",
                "away_score",
                "automatic_result",
            )
        })
    )
    results = []
    for values in query.values():
        snapshot = {
            field: values.pop(f"known_{field}")
            for field in (
                "id",
                "status",
                "home_score",
                "away_score",
                "automatic_result",
            )
        }
        # Transient reconstructed result for pure replay; never save this object.
        row = Match(**values)
        if snapshot["id"] is not None:
            for field in ("status", "home_score", "away_score", "automatic_result"):
                setattr(row, field, snapshot[field])
        elif row.result_observed_at is None or row.result_observed_at >= cutoff:
            continue
        if (
            row.status == "FINAL"
            and not row.automatic_result
            and row.home_score is not None
            and row.away_score is not None
        ):
            results.append(row)
    return results


def unavailable(reason: str) -> dict[str, Any]:
    """Keep the neutral score/time fallback explicit."""
    return {"status": "unavailable", "reason": reason}
