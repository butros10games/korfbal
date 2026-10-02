"""Persist cross-season club-team Elo and read it for rankings and predictions.

Every 15 minutes, matches changed since the previous refresh are replayed from the
earliest changed kickoff, resuming from the stored ratings at that moment; a match
weekend touches thousands of rows rather than the whole history. A nightly full
replay remains the reference: it also applies what an incremental refresh cannot
see cheaply (deleted matches, merged team identities, moved kickoffs, changes more
than ``INCREMENTAL_DAYS`` old) and recomputes comparison groups.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from apps.competition.domain.team_elo import (
    MODEL_VERSION,
    PROVISIONAL_GAMES,
    Fixture,
    Replay,
    TeamState,
    entering,
    expected_score,
    outcome_probabilities,
    replay,
)
from apps.competition.models import Match, MatchRating, TeamRating
from apps.schedule.models import Match as NativeMatch


WATERMARK_KEY = f"competition:{MODEL_VERSION}:refreshed-through"
# Session advisory lock: released by PostgreSQL if the worker dies mid-refresh.
LOCK_ID = 0x4B4F5246454C4F32
# Rows saved shortly before the previous run may have committed after it read.
OVERLAP = timedelta(minutes=10)
INCREMENTAL_DAYS = 60
BATCH = 5000
# PostgreSQL accepts at most 65,535 parameters per statement (13 per match row).
UPSERT_BATCH = 2000
MATCH_FIELDS = (
    "home_rating",
    "away_rating",
    "home_expected",
    "home_change",
    "home_games",
    "away_games",
    "home_team_id",
    "away_team_id",
    "starts_at",
    "phase_id",
    "home_phase_start",
    "away_phase_start",
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
RESUME_FIELDS = (
    "starts_at",
    "match_id",
    "phase_id",
    "home_rating",
    "away_rating",
    "home_change",
    "home_games",
    "away_games",
    "home_phase_start",
    "away_phase_start",
)


def linked() -> QuerySet[Match]:
    """Non-cup fixtures whose two teams are linked to native club teams."""
    return Match.objects.filter(
        cup_fixture__isnull=True,
        home_team__group__local_team__isnull=False,
        away_team__group__local_team__isnull=False,
    )


def fixtures(
    query: QuerySet[Match],
) -> tuple[list[Fixture], dict[str, int | None]]:
    """Load fixtures, plus each team's latest result class among them.

    Awarded results, missing scores and unverified 0-0 placeholders only add poule
    membership, matching the score forecast's result policy.
    """
    rows = query.values_list(
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
    ).iterator(chunk_size=BATCH)
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


@contextmanager
def refresh_lock() -> Iterator[bool]:
    """Let one refresh run at a time, without a lease that can expire mid-run.

    Yields:
        Whether this process holds the refresh lock.

    """
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        acquired = bool(cursor.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])


def refresh_team_ratings(*, full: bool = False) -> dict[str, Any]:
    """Apply changed results incrementally, or replay everything when asked."""
    with refresh_lock() as acquired:
        if not acquired:
            return {"status": "busy"}
        started = timezone.now()
        mark = cache.get(WATERMARK_KEY)
        if full or mark is None or not TeamRating.objects.exists():
            result = full_refresh()
        else:
            result = incremental_refresh(datetime.fromisoformat(mark), started)
        cache.set(WATERMARK_KEY, started.isoformat(), None)
        return result


def full_refresh() -> dict[str, Any]:
    """Replay the complete history and make both tables equal to it."""
    loaded, classes = fixtures(linked())
    state = replay(loaded)
    return {"status": "full", "teams": len(state.teams), **store(state, classes)}


def incremental_refresh(mark: datetime, now: datetime) -> dict[str, Any]:
    """Replay from the earliest recently changed kickoff onwards.

    Falls back to a full replay when stored ratings lack resume data.
    """
    changed = list(
        linked()
        .filter(updated_at__gt=mark - OVERLAP)
        .values_list("starts_at", flat=True)
    )
    recent = [
        kickoff
        for kickoff in changed
        if kickoff >= now - timedelta(days=INCREMENTAL_DAYS)
    ]
    deferred = len(changed) - len(recent)
    if not recent:
        return {"status": "unchanged", "deferred": deferred}
    since = min(recent)
    window, classes = fixtures(linked().filter(starts_at__gte=since))
    members = poule_members(window)
    teams = {team for fixture in window for team in (fixture.home, fixture.away)}
    teams |= {team for poule in members.values() for team in poule}
    initial = states_before(since, teams)
    if initial is None:
        return full_refresh()
    state = replay(window, initial=initial, members=members)
    written = store(state, classes, since=since)
    return {
        "status": "incremental",
        "since": since.isoformat(),
        "deferred": deferred,
        **written,
    }


def poule_members(window: list[Fixture]) -> dict[tuple[str, int | None], set[str]]:
    """Complete membership of every poule in the window, including earlier rounds."""
    pools = {fixture.pool for fixture in window if fixture.pool is not None}
    open_phases = {fixture.phase for fixture in window if fixture.pool is None}
    members: dict[tuple[str, int | None], set[str]] = defaultdict(set)
    rows = (
        linked()
        .filter(
            Q(pool_id__in=pools)
            | Q(pool__isnull=True, season_id__in=[UUID(p) for p in open_phases])
        )
        .values_list(
            "season_id",
            "pool_id",
            "home_team__group__local_team_id",
            "away_team__group__local_team_id",
        )
        .iterator(chunk_size=BATCH)
    )
    for season, pool, home, away in rows:
        members[str(season), pool] |= {str(home), str(away)}
    return members


def states_before(since: datetime, teams: set[str]) -> dict[str, TeamState] | None:
    """Rebuild each team's state just before ``since`` from stored ratings.

    Teams that last played earlier keep their stored rating; others resume after
    their last match before ``since``. ``None`` asks for a full replay.
    """
    stored = TeamRating.objects.filter(team_id__in=[UUID(team) for team in teams])
    states = {}
    resume = []
    for row in stored:
        state = TeamState(
            rating=row.rating,
            games=row.games,
            phase=str(row.phase_id),
            phase_start=row.phase_start,
            last_played=row.last_played_at,
            group=row.comparison_group,
        )
        if row.last_played_at < since:
            states[str(row.team_id)] = state
        else:
            resume.append(row.team_id)
    last = {}
    for side in ("home", "away"):
        latest = (
            MatchRating.objects
            .filter(**{f"{side}_team": OuterRef("team_id")}, starts_at__lt=since)
            .order_by("-starts_at", "-match_id")
            .values("match_id")[:1]
        )
        last[side] = dict(
            TeamRating.objects
            .filter(team_id__in=resume)
            .annotate(previous=Subquery(latest))
            .values_list("team_id", "previous")
        )
    groups = dict(
        TeamRating.objects.filter(team_id__in=resume).values_list(
            "team_id", "comparison_group"
        )
    )
    candidates = {
        row["match_id"]: row
        for row in MatchRating.objects.filter(
            match_id__in={
                match for side in last.values() for match in side.values() if match
            }
        ).values(*RESUME_FIELDS)
    }
    for team in resume:
        options = [
            (candidates[last[side][team]], side)
            for side in ("home", "away")
            if last[side].get(team) is not None
        ]
        if not options:
            continue
        row, side = max(
            options, key=lambda option: (option[0]["starts_at"], option[0]["match_id"])
        )
        phase_start = row[f"{side}_phase_start"]
        if phase_start is None or row["phase_id"] is None:
            return None
        change = row["home_change"] if side == "home" else -row["home_change"]
        states[str(team)] = TeamState(
            rating=row[f"{side}_rating"] + change,
            games=row[f"{side}_games"] + 1,
            phase=str(row["phase_id"]),
            phase_start=phase_start,
            last_played=row["starts_at"],
            group=groups[team],
        )
    return states


@transaction.atomic
def store(
    state: Replay, classes: dict[str, int | None], since: datetime | None = None
) -> dict[str, int]:
    """Write changed rows only; removed results and teams lose their ratings.

    Ratings are stored unrounded, so a refresh resuming from them reproduces a
    full replay exactly.

    An incremental refresh (``since``) rewrites ratings of matches from ``since``
    and of teams that played there; other rows are left to the full replay.
    """
    matches = {
        rating.match: MatchRating(
            match_id=rating.match,
            home_rating=rating.home_rating,
            away_rating=rating.away_rating,
            home_expected=rating.home_expected,
            home_change=rating.home_change,
            home_games=rating.home_games,
            away_games=rating.away_games,
            home_team_id=UUID(rating.home),
            away_team_id=UUID(rating.away),
            starts_at=rating.starts_at,
            phase_id=UUID(rating.phase),
            home_phase_start=rating.home_phase_start,
            away_phase_start=rating.away_phase_start,
        )
        for rating in state.matches
    }
    teams = {
        UUID(team): TeamRating(
            team_id=UUID(team),
            rating=value.rating,
            phase_start=value.phase_start,
            games=value.games,
            phase_id=UUID(value.phase),
            last_played_at=value.last_played,
            competition_class_id=classes.get(team),
            comparison_group=value.group,
            model=MODEL_VERSION,
        )
        for team, value in state.teams.items()
        if since is None or (value.last_played and value.last_played >= since)
    }
    match_scope = MatchRating.objects.all()
    team_scope = TeamRating.objects.all()
    if since is not None:
        match_scope = match_scope.filter(starts_at__gte=since)
        team_scope = team_scope.filter(team_id__in=teams)
    written = {}
    for name, model_rows, scope, key, fields in (
        ("matches", matches, match_scope, "match_id", MATCH_FIELDS),
        ("teams", teams, team_scope, "team_id", TEAM_FIELDS),
    ):
        for action, count in synchronize(scope, key, model_rows, fields).items():
            written[f"{name}_{action}"] = count
    return written


def synchronize(
    scope: QuerySet[MatchRating] | QuerySet[TeamRating],
    key: str,
    wanted: dict,
    fields: tuple[str, ...],
) -> dict[str, int]:
    """Insert, update and delete so the scoped rows equal the replay."""
    existing = {
        row[key]: tuple(row[field] for field in fields)
        for row in scope.values(key, *fields).iterator(chunk_size=BATCH)
    }
    created = [row for pk, row in wanted.items() if pk not in existing]
    changed = [
        row
        for pk, row in wanted.items()
        if pk in existing
        and existing[pk] != tuple(getattr(row, field) for field in fields)
    ]
    stale = [pk for pk in existing if pk not in wanted]
    # One upsert writes new and changed rows in linear time. ``bulk_update`` sends
    # a CASE branch per row for every field, which PostgreSQL evaluates per row:
    # rewriting the whole history held one statement for over 25 minutes.
    scope.model.objects.bulk_create(
        [*created, *changed],
        batch_size=UPSERT_BATCH,
        update_conflicts=True,
        unique_fields=[scope.model._meta.pk.name],
        update_fields=[field.removesuffix("_id") for field in fields],
    )
    for start in range(0, len(stale), BATCH):
        scope.model.objects.filter(pk__in=stale[start : start + BATCH]).delete()
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
