"""Persist cross-season club-team Elo and read it for rankings and predictions.

The replay reads every non-cup fixture once and rewrites only ratings that changed,
so a correction to an old result propagates to every later match it affects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Max, Sum

from apps.competition.domain.team_elo import (
    MODEL_VERSION,
    PROVISIONAL_GAMES,
    Fixture,
    Replay,
    entering,
    expected_score,
    outcome_probabilities,
    replay,
)
from apps.competition.models import Match, MatchRating, TeamRating
from apps.schedule.models import Match as NativeMatch


LOCK_KEY = f"competition:{MODEL_VERSION}:refresh"
FINGERPRINT_KEY = f"competition:{MODEL_VERSION}:fingerprint"
LOCK_SECONDS = 900
BATCH = 5000
DIGITS = 3
MATCH_FIELDS = (
    "home_rating",
    "away_rating",
    "home_expected",
    "home_change",
    "home_games",
    "away_games",
)
TEAM_FIELDS = (
    "rating",
    "phase_start",
    "games",
    "phase_id",
    "last_played_at",
    "competition_class_id",
    "comparison_group",
    "model",
)


def fixtures() -> tuple[list[Fixture], dict[str, int | None]]:
    """Load every linked non-cup fixture, plus each team's latest result class.

    Awarded results, missing scores and unverified 0-0 placeholders only add poule
    membership, matching the score forecast's result policy.
    """
    rows = (
        Match.objects
        .filter(
            cup_fixture__isnull=True,
            home_team__group__local_team__isnull=False,
            away_team__group__local_team__isnull=False,
        )
        .values_list(
            "pk",
            "season_id",
            "pool_id",
            "starts_at",
            "home_team__group__local_team_id",
            "away_team__group__local_team_id",
            "status",
            "automatic_result",
            "home_score",
            "away_score",
            "pool__competition_class_id",
        )
        .iterator(chunk_size=BATCH)
    )
    loaded = []
    latest: dict[str, tuple[datetime, int | None]] = {}
    for pk, season, pool, starts_at, home, away, status, awarded, hs, aw, kind in rows:
        result = (
            status == "FINAL"
            and not awarded
            and hs is not None
            and aw is not None
            and (hs, aw) != (0, 0)
        )
        fixture = Fixture(
            match=pk,
            phase=str(season),
            pool=pool,
            starts_at=starts_at,
            home=str(home),
            away=str(away),
            home_score=hs if result else None,
            away_score=aw if result else None,
        )
        loaded.append(fixture)
        if result:
            for team in (fixture.home, fixture.away):
                if team not in latest or latest[team][0] <= starts_at:
                    latest[team] = (starts_at, kind)
    return loaded, {team: kind for team, (_, kind) in latest.items()}


def fingerprint() -> str:
    """Change whenever a fixture is added, removed or its content changes.

    Score sums also catch direct corrections that bypass ``updated_at``.
    """
    summary = Match.objects.aggregate(
        count=Count("pk"),
        changed=Max("updated_at"),
        home=Sum("home_score"),
        away=Sum("away_score"),
    )
    changed = summary["changed"]
    stamp = changed.isoformat() if changed else "empty"
    return f"{summary['count']}:{stamp}:{summary['home']}:{summary['away']}"


def refresh_team_ratings(*, force: bool = False) -> dict[str, Any]:
    """Replay the full history when results changed; one refresh runs at a time."""
    owner = str(uuid4())
    if not cache.add(LOCK_KEY, owner, LOCK_SECONDS):
        return {"status": "busy"}
    try:
        stamp = fingerprint()
        if not force and cache.get(FINGERPRINT_KEY) == stamp:
            return {"status": "unchanged"}
        loaded, classes = fixtures()
        state = replay(loaded)
        written = store(state, classes)
        cache.set(FINGERPRINT_KEY, stamp, None)
        return {"status": "refreshed", "teams": len(state.teams), **written}
    finally:
        if cache.get(LOCK_KEY) == owner:
            cache.delete(LOCK_KEY)


@transaction.atomic
def store(state: Replay, classes: dict[str, int | None]) -> dict[str, int]:
    """Write changed rows only; removed results and teams lose their ratings."""
    matches = {
        rating.match: MatchRating(
            match_id=rating.match,
            home_rating=round(rating.home_rating, DIGITS),
            away_rating=round(rating.away_rating, DIGITS),
            home_expected=round(rating.home_expected, DIGITS + 3),
            home_change=round(rating.home_change, DIGITS),
            home_games=rating.home_games,
            away_games=rating.away_games,
        )
        for rating in state.matches
    }
    teams = {
        UUID(team): TeamRating(
            team_id=UUID(team),
            rating=round(value.rating, DIGITS),
            phase_start=round(value.phase_start, DIGITS),
            games=value.games,
            phase_id=UUID(value.phase),
            last_played_at=value.last_played,
            competition_class_id=classes.get(team),
            comparison_group=value.group,
            model=MODEL_VERSION,
        )
        for team, value in state.teams.items()
    }
    return {
        **{
            f"matches_{key}": count
            for key, count in synchronize(
                MatchRating, "match_id", matches, MATCH_FIELDS
            ).items()
        },
        **{
            f"teams_{key}": count
            for key, count in synchronize(
                TeamRating, "team_id", teams, TEAM_FIELDS
            ).items()
        },
    }


def synchronize(
    model: type[MatchRating | TeamRating],
    key: str,
    wanted: dict,
    fields: tuple[str, ...],
) -> dict[str, int]:
    """Insert, update and delete so the table equals the replay."""
    existing = {
        row[key]: tuple(row[field] for field in fields)
        for row in model.objects.values(key, *fields).iterator(chunk_size=BATCH)
    }
    created = [row for pk, row in wanted.items() if pk not in existing]
    changed = [
        row
        for pk, row in wanted.items()
        if pk in existing
        and existing[pk] != tuple(getattr(row, field) for field in fields)
    ]
    stale = [pk for pk in existing if pk not in wanted]
    model.objects.bulk_create(created, batch_size=BATCH)
    model.objects.bulk_update(
        changed,
        [field.removesuffix("_id") for field in fields],
        batch_size=BATCH,
    )
    for start in range(0, len(stale), BATCH):
        model.objects.filter(pk__in=stale[start : start + BATCH]).delete()
    return {"created": len(created), "updated": len(changed), "deleted": len(stale)}


def elo_prediction(
    match: NativeMatch, source: Match, cutoff: datetime
) -> dict[str, Any] | None:
    """Predict from ratings known at kickoff: stored for played matches, else current.

    A team that has not yet played in the fixture's phase enters it exactly as the
    replay will: anchored on the current ratings of its new poule.
    """
    home_team, away_team = source.home_team.group, source.away_team.group
    if (
        home_team is None
        or away_team is None
        or home_team.local_team_id != match.home_team_id
        or away_team.local_team_id != match.away_team_id
    ):
        return None
    stored = MatchRating.objects.filter(match=source).first()
    if stored is not None:
        home, away = stored.home_rating, stored.away_rating
        games = (stored.home_games, stored.away_games)
    else:
        assert home_team.local_team_id is not None
        assert away_team.local_team_id is not None
        current = upcoming(source, home_team.local_team_id, away_team.local_team_id)
        if current is None:
            return None
        (home, home_games), (away, away_games) = current
        games = (home_games, away_games)
    expected = expected_score(home, away)
    context = source.pool.competition_class if source.pool else None
    return {
        "status": "seeded",
        "model": MODEL_VERSION,
        "as_of": cutoff.isoformat(),
        "home_expected_result": expected,
        "home_rating": round(home, 1),
        "away_rating": round(away, 1),
        "home_games": games[0],
        "away_games": games[1],
        "discipline": "indoor" if "-ZA-" in source.home_team.sport else "outdoor",
        "category": context.category if context else "unknown",
        "class_code": context.code if context else "unknown",
        "provisional": min(games) < PROVISIONAL_GAMES,
        "outcome_calibration": {
            "model": MODEL_VERSION,
            **outcome_probabilities(expected),
        },
    }


def upcoming(
    source: Match, home: UUID, away: UUID
) -> tuple[tuple[float, int], tuple[float, int]] | None:
    """Return current ratings, moved into the fixture's phase if new there."""
    ratings = {
        row.team_id: row for row in TeamRating.objects.filter(team_id__in=(home, away))
    }
    if home not in ratings or away not in ratings:
        return None
    poule = None
    result = []
    for team in (home, away):
        rating = ratings[team]
        value = rating.rating
        if rating.phase_id != source.season_id:
            if poule is None:
                poule = poule_ratings(source)
            value = entering(value, poule)
        result.append((value, rating.games))
    return result[0], result[1]


def poule_ratings(source: Match) -> list[float]:
    """Return current ratings of the club teams scheduled in the fixture's poule."""
    members = Match.objects.filter(
        season_id=source.season_id, pool_id=source.pool_id, cup_fixture__isnull=True
    ).values_list("home_team__group__local_team_id", "away_team__group__local_team_id")
    teams = {team for pair in members for team in pair if team is not None}
    return [
        rating
        for _, rating in sorted(
            TeamRating.objects.filter(team_id__in=teams).values_list(
                "team_id", "rating"
            ),
            key=lambda row: str(row[0]),
        )
    ]
